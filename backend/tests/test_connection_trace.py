"""「测试连接」的分步过程。

以前这个接口只回 ``{success, message}``:界面按下去干等几秒才蹦个提示,失败了
也只知道「失败」—— 到底是配置就不全、TCP 没通、还是连上了但设备不响应,这三种
情况的排障动作完全不同,却给同一句话。

顺带锁住一个更要命的问题:视图里曾经**手搓**一份只有 modbus 字段的配置
(source_ip / source_port / slave_id / byte_order / timeout),于是 MQTT 没有
账号密码、OPC-UA 没有 endpoint_url、S7 没有 rack/slot —— 非 Modbus 协议
「测试连接」测的根本不是它自己的配置,必然失败,而且失败原因还完全是误导的。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from acquisition.protocols import ProtocolRegistry
from acquisition.protocols.base import BaseProtocol, FieldSpec, ProtocolMeta
from acquisition.services.connection_trace import trace_connection
from tests.fixtures.factories import *  # noqa: F401,F403


STEP_KEYS = ["config", "connect", "handshake", "disconnect"]


def _by_key(result):
    return {s["key"]: s for s in result["steps"]}


# ---------------------------------------------------------------------------
# 测试用协议:连接/健康检查的行为可控
# ---------------------------------------------------------------------------


class _Probe(BaseProtocol):
    META = ProtocolMeta(name="probe", label="探针", category="other")
    DEVICE_FIELDS = (
        FieldSpec("source_ip", "IP 地址", required=True),
        FieldSpec("token", "令牌", kind="secret", required=True),
    )
    IDENTITY_FIELDS = ("source_ip",)

    #: 类级开关,用例按需拨
    connect_raises = None
    connect_returns = True
    healthy = True
    health_raises = None

    def connect(self):
        if _Probe.connect_raises:
            raise _Probe.connect_raises
        self.is_connected = bool(_Probe.connect_returns)
        return _Probe.connect_returns

    def disconnect(self):
        self.is_connected = False

    def read_points(self, points):
        return []

    def health_check(self):
        if _Probe.health_raises:
            raise _Probe.health_raises
        return _Probe.healthy


@pytest.fixture(autouse=True)
def _register_probe():
    ProtocolRegistry._protocols["probe"] = _Probe
    _Probe.connect_raises = None
    _Probe.connect_returns = True
    _Probe.healthy = True
    _Probe.health_raises = None
    yield
    ProtocolRegistry._protocols.pop("probe", None)


GOOD_CONFIG = {"source_ip": "10.0.0.1", "token": "t"}


# ---------------------------------------------------------------------------
# 过程本身
# ---------------------------------------------------------------------------


def test_happy_path_reports_every_step():
    result = trace_connection("probe", GOOD_CONFIG, device_code="dev-1")

    assert result["success"] is True
    assert result["summary"] == "连接正常"
    assert [s["key"] for s in result["steps"]] == STEP_KEYS
    assert all(s["status"] == "ok" for s in result["steps"])
    # 每步都要有耗时,界面要显示「卡在哪一步、卡了多久」
    assert all("duration_ms" in s for s in result["steps"])


def test_missing_required_config_fails_before_touching_the_network():
    """配置就不全时不该去连 —— 而且要指名道姓缺哪个字段。"""
    result = trace_connection("probe", {"source_ip": "10.0.0.1"})

    steps = _by_key(result)
    assert result["success"] is False
    assert steps["config"]["status"] == "failed"
    assert "令牌" in steps["config"]["detail"]
    # 后面三步都没发生,如实标成 skipped 而不是 failed
    assert steps["connect"]["status"] == "skipped"
    assert steps["handshake"]["status"] == "skipped"
    assert steps["disconnect"]["status"] == "skipped"


def test_connect_failure_is_isolated_to_that_step():
    _Probe.connect_raises = ConnectionError("Connection refused")
    result = trace_connection("probe", GOOD_CONFIG)

    steps = _by_key(result)
    assert result["success"] is False
    assert steps["config"]["status"] == "ok"
    assert steps["connect"]["status"] == "failed"
    assert "Connection refused" in steps["connect"]["detail"]
    assert steps["handshake"]["status"] == "skipped"
    assert "建立连接失败" in result["summary"]


def test_connect_returning_false_counts_as_failure():
    """有的协议连不上是返回 False 而不是抛异常,不能当成功。"""
    _Probe.connect_returns = False
    result = trace_connection("probe", GOOD_CONFIG)

    assert result["success"] is False
    assert _by_key(result)["connect"]["status"] == "failed"


def test_connected_but_unhealthy_is_a_warning_not_a_hard_failure():
    """连上了但设备不响应 —— 和连不上完全是两码事,排障方向也不同。"""
    _Probe.healthy = False
    result = trace_connection("probe", GOOD_CONFIG)

    steps = _by_key(result)
    assert steps["connect"]["status"] == "ok"
    assert steps["handshake"]["status"] == "warning"
    assert result["success"] is False
    assert "未通过健康检查" in result["summary"]
    # 连上了就得断开,别把连接漏在那
    assert steps["disconnect"]["status"] == "ok"


def test_health_check_raising_is_reported_on_that_step():
    _Probe.health_raises = TimeoutError("read timeout")
    result = trace_connection("probe", GOOD_CONFIG)

    steps = _by_key(result)
    assert steps["handshake"]["status"] == "failed"
    assert "read timeout" in steps["handshake"]["detail"]


def test_trace_never_raises_even_for_unknown_protocol():
    """接口拿到的永远是一份完整过程,不能让异常冒到视图去。"""
    result = trace_connection("no_such_protocol", {})

    assert result["success"] is False
    assert _by_key(result)["config"]["status"] == "failed"


# ---------------------------------------------------------------------------
# 接口层
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_endpoint_returns_steps(create_device):
    device = create_device(protocol="probe", ip="10.0.0.1",
                           metadata={"source_ip": "10.0.0.1", "token": "t"})

    resp = APIClient().post(f"/api/config/devices/{device.id}/test-connection/")

    assert resp.status_code == 200
    assert resp.data["success"] is True
    assert [s["key"] for s in resp.data["steps"]] == STEP_KEYS
    assert resp.data["summary"] == "连接正常"
    # 旧字段保留,老调用方不至于一起改
    assert resp.data["message"] == resp.data["summary"]


@pytest.mark.django_db
def test_endpoint_uses_the_real_layered_config_not_a_modbus_shaped_stub(create_device):
    """核心回归:必须走 build_device_config。

    视图以前手搓 ``{source_ip, source_port, slave_id, byte_order, timeout}``,
    非 Modbus 协议的字段(这里的 token,现实中的 MQTT 账号密码 / OPC-UA
    endpoint_url / S7 rack-slot)全都丢了,测试连接必然失败。
    """
    device = create_device(protocol="probe", ip="10.0.0.1",
                           metadata={"source_ip": "10.0.0.1", "token": "secret-token"})

    seen = {}

    def _capture(args=None, **kwargs):
        seen["config"] = args[1]
        from unittest.mock import MagicMock
        result = MagicMock()
        result.get.return_value = trace_connection(args[0], args[1], device_code=args[2])
        return result

    with patch("acquisition.tasks.trace_protocol_connection.apply_async", _capture):
        resp = APIClient().post(f"/api/config/devices/{device.id}/test-connection/")

    assert seen["config"].get("token") == "secret-token", (
        f"协议专属字段没传下去,拿到的是: {sorted(seen['config'])}"
    )
    assert resp.data["success"] is True


@pytest.mark.django_db
def test_endpoint_caps_timeout_for_interactive_use(create_device):
    """交互操作最多等 5s,别让人对着转圈干等。"""
    device = create_device(protocol="probe", ip="10.0.0.1",
                           metadata={"source_ip": "10.0.0.1", "token": "t",
                                     "timeout": 120})

    seen = {}

    def _capture(args=None, **kwargs):
        seen["config"] = args[1]
        from unittest.mock import MagicMock
        result = MagicMock()
        result.get.return_value = trace_connection(args[0], args[1])
        return result

    with patch("acquisition.tasks.trace_protocol_connection.apply_async", _capture):
        APIClient().post(f"/api/config/devices/{device.id}/test-connection/")

    assert seen["config"]["timeout"] == 5.0
