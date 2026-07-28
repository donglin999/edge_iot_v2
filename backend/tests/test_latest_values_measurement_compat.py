"""latest-values 查询端兼容新旧 measurement（只加不改）。

背景：scada 入库 measurement 从 device.code 切到设备编号
（device.metadata["device_a_tag"]），历史数据仍在旧 measurement 下。
查询端点必须同时命中新旧两种 measurement：

- 有 device_a_tag：flux 里 device.code 和 device_a_tag 两个条件 or 起来，
  并在 last() 前显式 sort(_time) 保证跨 measurement 取全局最新。
- 无 device_a_tag：flux 与改造前逐字节一致（回归保护）。
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from rest_framework.test import APIClient

from configuration.views import _candidate_measurements, _measurement_filter_expr
from tests.fixtures.factories import *  # noqa: F401,F403


OLD_CODE = "scada-zs-bochuang-A0201010001150403"
A_TAG = "A0201010001150403"


@pytest.fixture
def api_client():
    return APIClient()


def _mock_storage_registry(mock_registry, records=None):
    """StorageRegistry.create() -> mock storage，query() 返回给定 records。"""
    mock_storage = MagicMock()
    mock_storage.query.return_value = records or []
    mock_registry.create.return_value = mock_storage
    return mock_storage


# ------------------------------------------------------------------
# 纯函数层
# ------------------------------------------------------------------
class TestCandidateMeasurements:
    def _device(self, code=OLD_CODE, metadata=None):
        return SimpleNamespace(code=code, metadata=metadata)

    def test_no_metadata(self):
        assert _candidate_measurements(self._device(metadata=None)) == [OLD_CODE]
        assert _candidate_measurements(self._device(metadata={})) == [OLD_CODE]

    def test_with_device_a_tag(self):
        dev = self._device(metadata={"device_a_tag": A_TAG})
        assert _candidate_measurements(dev) == [OLD_CODE, A_TAG]

    def test_blank_or_duplicate_tag_ignored(self):
        assert _candidate_measurements(
            self._device(metadata={"device_a_tag": "   "})
        ) == [OLD_CODE]
        assert _candidate_measurements(
            self._device(code=A_TAG, metadata={"device_a_tag": A_TAG})
        ) == [A_TAG]

    def test_non_string_tag_ignored(self):
        assert _candidate_measurements(
            self._device(metadata={"device_a_tag": 12345})
        ) == [OLD_CODE]

    def test_filter_expr_escapes_and_joins(self):
        expr = _measurement_filter_expr(['a"b', "c\\d"])
        assert expr == 'r["_measurement"] == "a\\"b" or r["_measurement"] == "c\\\\d"'


# ------------------------------------------------------------------
# Device /latest-values 端点
# ------------------------------------------------------------------
@pytest.mark.django_db
class TestDeviceLatestValuesMeasurementCompat:
    def test_flux_contains_both_measurements(self, api_client, create_device):
        device = create_device(code=OLD_CODE, metadata={"device_a_tag": A_TAG})
        with patch("storage.StorageRegistry") as mock_registry:
            storage = _mock_storage_registry(mock_registry)
            response = api_client.get(
                f"/api/config/devices/{device.id}/latest-values/"
            )

        assert response.status_code == 200
        flux = storage.query.call_args[0][0]
        assert f'r["_measurement"] == "{OLD_CODE}"' in flux
        assert f'r["_measurement"] == "{A_TAG}"' in flux
        assert " or " in flux
        # 跨 measurement 时必须先 sort 再 last，才是全局最新
        assert flux.index('sort(columns: ["_time"])') < flux.index("last()")

    def test_flux_unchanged_without_tag(self, api_client, create_device):
        """无 device_a_tag 时查询与改造前逐字节一致（只加不改）。"""
        device = create_device(code="PLAIN_DEV")
        with patch("storage.StorageRegistry") as mock_registry:
            storage = _mock_storage_registry(mock_registry)
            response = api_client.get(
                f"/api/config/devices/{device.id}/latest-values/"
            )

        assert response.status_code == 200
        flux = storage.query.call_args[0][0]
        expected_tail = (
            '|> filter(fn: (r) => r["_measurement"] == "PLAIN_DEV") '
            '|> group(columns: ["_field"]) '
            "|> last()"
        )
        assert flux.endswith(expected_tail)
        assert "sort" not in flux

    def test_records_from_either_measurement_merge(
        self, api_client, create_device, create_point
    ):
        """两个 measurement 里的同名 _field 经 last() 后只剩一条，值照常合并。"""
        device = create_device(code=OLD_CODE, metadata={"device_a_tag": A_TAG})
        create_point(device=device, code="temp1")
        records = [
            {"_field": "temp1", "_value": 42.5, "_time": None, "quality": "good"},
        ]
        with patch("storage.StorageRegistry") as mock_registry:
            _mock_storage_registry(mock_registry, records)
            response = api_client.get(
                f"/api/config/devices/{device.id}/latest-values/"
            )

        assert response.status_code == 200
        by_code = {p["point_code"]: p for p in response.data["points"]}
        assert by_code["temp1"]["value"] == 42.5


# ------------------------------------------------------------------
# Points /latest-values 端点（按 device 分组逐个查）
# ------------------------------------------------------------------
@pytest.mark.django_db
class TestPointsLatestValuesMeasurementCompat:
    def test_flux_contains_both_measurements(
        self, api_client, create_device, create_point
    ):
        device = create_device(code=OLD_CODE, metadata={"device_a_tag": A_TAG})
        create_point(device=device, code="temp1")
        with patch("storage.StorageRegistry") as mock_registry:
            storage = _mock_storage_registry(mock_registry)
            response = api_client.get(
                f"/api/config/points/latest-values/?device_id={device.id}"
            )

        assert response.status_code == 200
        flux = storage.query.call_args[0][0]
        assert f'r["_measurement"] == "{OLD_CODE}"' in flux
        assert f'r["_measurement"] == "{A_TAG}"' in flux
        assert flux.index('sort(columns: ["_time"])') < flux.index("last()")

    def test_flux_unchanged_without_tag(
        self, api_client, create_device, create_point
    ):
        device = create_device(code="PLAIN_DEV2")
        create_point(device=device, code="temp1")
        with patch("storage.StorageRegistry") as mock_registry:
            storage = _mock_storage_registry(mock_registry)
            response = api_client.get(
                f"/api/config/points/latest-values/?device_id={device.id}"
            )

        assert response.status_code == 200
        flux = storage.query.call_args[0][0]
        expected_tail = (
            '|> filter(fn: (r) => r["_measurement"] == "PLAIN_DEV2") '
            '|> group(columns: ["_field"]) '
            "|> last()"
        )
        assert flux.endswith(expected_tail)
        assert "sort" not in flux
