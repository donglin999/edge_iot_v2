"""point-history 可选 ``window`` 参数(按窗口取最新一条,last 降采样)。

覆盖三类行为(team-prompts scada-topic-and-storage / Agent D):
  1. 合法窗口 → aggregateWindow(every: …, fn: last, createEmpty: false) 进入
     Flux 查询串,且位置在 filter 之后、sort/limit 之前;
  2. 非法窗口(注入 / 变形 / 越界)→ 400,查询根本不会发出;
  3. 缺省(不带 window)→ 查询串与原实现逐字节一致(锁死"只加不改")。
"""
from unittest.mock import MagicMock, patch

import pytest
from django.conf import settings as dj_settings
from rest_framework import status
from rest_framework.test import APIClient

from acquisition.views import _validate_flux_window

ENDPOINT = "/api/acquisition/sessions/point-history/"


@pytest.fixture
def api_client():
    return APIClient()


def _query_via_mock(api_client, params):
    """调 point-history 并捕获发给 storage.query 的 Flux 串。"""
    with patch("storage.StorageRegistry.create") as mock_create:
        mock_storage = MagicMock()
        mock_storage.query.return_value = []
        mock_create.return_value = mock_storage
        resp = api_client.get(ENDPOINT, params)
    if mock_storage.query.call_args is None:
        return resp, None
    return resp, mock_storage.query.call_args.args[0]


# ---------------------------------------------------------------------------
# 1. 校验函数本体
# ---------------------------------------------------------------------------


class TestFluxWindowValidation:
    @pytest.mark.parametrize("value", [
        "10s", "30s", "1m", "5m", "500ms", "2h", "24h", "  10s  ",
    ])
    def test_valid_windows_pass(self, value):
        assert _validate_flux_window(value) == value.strip()

    @pytest.mark.parametrize("payload", [
        # Flux 注入:闭合 aggregateWindow() 再塞任意管道。
        '10s, fn: last) |> drop(columns: ["_value"]) |> limit(n: 1',
        "1m) |> yield(name: \"x\"",
        "10s; import \"http\"",
        # 变形 / 非白名单单位。
        "-10s",          # 负窗口
        "+10s",
        "10d",           # 天不在白名单
        "10w",
        "1h30m",         # 复合 duration
        "10",            # 没单位
        "s",             # 没数字
        "foobar",
        "",
        "   ",
        # 数值不合理。
        "0s",            # 零窗口
        "0ms",
        "25h",           # 超过 24h 上限
        "999999999s",
        None,
    ])
    def test_injection_and_malformed_rejected(self, payload):
        with pytest.raises(ValueError):
            _validate_flux_window(payload)


# ---------------------------------------------------------------------------
# 2. 端点行为
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPointHistoryWindowParam:
    @pytest.mark.parametrize("window", ["10s", "30s", "1m", "5m"])
    def test_valid_window_lands_in_flux_between_filter_and_sort(
        self, api_client, window
    ):
        resp, flux = _query_via_mock(
            api_client,
            {"point_code": "Temp_01", "start_time": "-1h", "window": window},
        )
        assert resp.status_code == status.HTTP_200_OK
        agg = f'|> aggregateWindow(every: {window}, fn: last, createEmpty: false)'
        assert agg in flux
        # 位置锁死:filter 之后、sort/limit 之前。
        assert flux.index('|> filter(') < flux.index(agg)
        assert flux.index(agg) < flux.index('|> sort(')
        assert flux.index('|> sort(') < flux.index('|> limit(')

    @pytest.mark.parametrize("window", [
        '10s, fn: last) |> drop(columns: ["_value"]) |> limit(n: 1',
        "-10s",
        "10d",
        "0s",
        "25h",
        "foobar",
        "",
    ])
    def test_invalid_window_rejected_400_without_querying(self, api_client, window):
        resp, flux = _query_via_mock(
            api_client,
            {"point_code": "Temp_01", "start_time": "-1h", "window": window},
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert flux is None  # 非法参数绝不能触达 InfluxDB

    def test_without_window_query_keeps_newest_n(self, api_client):
        """缺省查询取**最新** N 条:group 合表 → 倒序 limit → 升序还原。

        (原「逐字节一致」契约随截尾方向修复更新:升序 sort+limit 取的是最旧
        N 条,现场高频推送下图的尾巴被砍 —— 2026-07-28 现场实锤。)
        """
        resp, flux = _query_via_mock(
            api_client,
            {"point_code": "Temp_01", "start_time": "-2h", "end_time": "now()"},
        )
        assert resp.status_code == status.HTTP_200_OK
        bucket = getattr(dj_settings, "INFLUXDB_BUCKET", "default")
        assert flux == (
            f'from(bucket:"{bucket}") '
            f'|> range(start: -2h, stop: now()) '
            f'|> filter(fn: (r) => r["_field"] == "Temp_01") '
            f'|> group() '
            f'|> sort(columns: ["_time"], desc: true) '
            f'|> limit(n: 1000) '
            f'|> sort(columns: ["_time"])'
        )


# ---------------------------------------------------------------------------
# full=1 自适应完整形态(count → 全量 or 极值包络)
# ---------------------------------------------------------------------------


def _query_full_via_mock(api_client, params, count_value):
    """Mock storage:第一次 query 回 count,第二次回空数据;捕获两条 flux。"""
    captured = []

    class _FakeStorage:
        def connect(self): pass
        def disconnect(self): pass
        def query(self, flux):
            captured.append(flux)
            if len(captured) == 1 and "count()" in flux:
                return [{"_value": count_value}]
            return []

    with patch("storage.StorageRegistry.create", return_value=_FakeStorage()):
        resp = api_client.get("/api/acquisition/sessions/point-history/", params)
    return resp, captured


@pytest.mark.django_db
class TestFullAdaptiveMode:
    def test_small_count_returns_untruncated_raw(self, api_client):
        resp, flux = _query_full_via_mock(
            api_client, {"point_code": "P1", "start_time": "-1h", "full": "1"},
            count_value=500,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.data["downsampled"] is False
        assert resp.data["raw_count"] == 500
        assert len(flux) == 2
        assert "count()" in flux[0]
        # 全量路径:无 limit 截尾、无聚合
        assert "limit(" not in flux[1]
        assert "aggregateWindow" not in flux[1]

    def test_large_count_switches_to_minmax_envelope(self, api_client):
        resp, flux = _query_full_via_mock(
            api_client, {"point_code": "P1", "start_time": "-1h", "full": "1"},
            count_value=50_000,
        )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.data["downsampled"] is True
        assert resp.data["raw_count"] == 50_000
        # 1h/1000 桶 → 3s 窗
        assert resp.data["window_used"] == "3s"
        env = flux[1]
        assert "fn: min" in env and "fn: max" in env and "union(" in env
        assert 'timeSrc: "_start"' in env

    def test_explicit_window_wins_over_full(self, api_client):
        resp, flux = _query_full_via_mock(
            api_client,
            {"point_code": "P1", "start_time": "-1h", "full": "1", "window": "30s"},
            count_value=0,
        )
        assert resp.status_code == status.HTTP_200_OK
        # window 显式给定:走 last 降采样,不发 count 查询
        assert len(flux) == 1
        assert "fn: last" in flux[0]
        assert resp.data["downsampled"] is True
        assert resp.data["window_used"] == "30s"

    def test_without_full_behavior_unchanged(self, api_client):
        resp, flux = _query_full_via_mock(
            api_client, {"point_code": "P1", "start_time": "-1h"},
            count_value=0,
        )
        assert len(flux) == 1
        assert "count()" not in flux[0]
        assert "limit(n: 1000)" in flux[0]
        assert resp.data["downsampled"] is False
