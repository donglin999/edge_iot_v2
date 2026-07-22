"""模拟设备协议 —— 无硬件联调用的假设备。

两件事必须锁死:

1. **默认不注册**。它是个假协议,生产环境的协议下拉里绝不能出现。开关是
   ``EDGE_ENABLE_SIMULATOR``,忘了这条就会有人在现场看到「模拟设备」选项。
2. **故障注入真的会失败**。它存在的意义有一半是演练掉线告警与自动重连 ——
   如果 fail 模式其实不失败,那演练出来的「一切正常」是假的。
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from acquisition.protocols import ProtocolRegistry
from acquisition.protocols.base import ConnectionError as ProtoConnectionError
from acquisition.protocols.base import ReadError
from acquisition.protocols.simulator import SimulatorProtocol, register_if_enabled


POINTS = [
    {"code": "temperature", "data_type": "float"},
    {"code": "pressure", "data_type": "float"},
    {"code": "running", "data_type": "bool"},
]


def _proto(**overrides) -> SimulatorProtocol:
    config = {"source_ip": "sim-1", "sim_waveform": "sine", "sim_period_s": 60,
              "sim_amplitude": 20, "sim_baseline": 50}
    config.update(overrides)
    return SimulatorProtocol(config)


# ---------------------------------------------------------------------------
# 注册开关
# ---------------------------------------------------------------------------


def test_not_registered_by_default():
    """核心回归:没开开关时,注册表里不该有它。"""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("EDGE_ENABLE_SIMULATOR", None)
        ProtocolRegistry._protocols.pop("simulator", None)
        assert register_if_enabled() is False
        assert "simulator" not in ProtocolRegistry._protocols


def test_registers_only_when_env_is_on():
    try:
        with patch.dict(os.environ, {"EDGE_ENABLE_SIMULATOR": "1"}):
            assert register_if_enabled() is True
            assert ProtocolRegistry._protocols["simulator"] is SimulatorProtocol
    finally:
        ProtocolRegistry._protocols.pop("simulator", None)


def test_simulator_is_not_in_the_user_facing_protocol_list_by_default():
    """前端协议下拉读的是 describe_all() —— 那里不能有假协议。"""
    ProtocolRegistry._protocols.pop("simulator", None)
    names = {p["name"] for p in ProtocolRegistry.describe_all()}
    assert "simulator" not in names


# ---------------------------------------------------------------------------
# 正常读数
# ---------------------------------------------------------------------------


def test_connect_read_disconnect():
    proto = _proto()
    assert proto.connect() is True
    assert proto.is_connected

    readings = proto.read_points(POINTS)
    assert len(readings) == len(POINTS)
    assert all(r["quality"] == "good" for r in readings)
    assert all(r["timestamp"] > 0 for r in readings)

    proto.disconnect()
    assert proto.is_connected is False


def test_data_type_is_honoured():
    proto = _proto()
    proto.connect()
    by_code = {r["code"]: r["value"] for r in proto.read_points(POINTS)}

    assert isinstance(by_code["temperature"], float)
    assert isinstance(by_code["running"], bool)


def test_points_do_not_all_return_the_same_value():
    """同一台设备的几个测点要能分辨,否则图上是重合的一条线。"""
    proto = _proto()
    proto.connect()
    values = [r["value"] for r in proto.read_points(
        [{"code": f"pt{i}", "data_type": "float"} for i in range(6)]
    )]
    assert len(set(values)) > 1


def test_constant_waveform_is_actually_constant():
    proto = _proto(sim_waveform="constant", sim_baseline=42.0)
    proto.connect()
    values = {r["value"] for r in proto.read_points(POINTS[:2])}
    assert values == {42.0}


def test_reading_without_connecting_is_an_error():
    with pytest.raises(ReadError):
        _proto().read_points(POINTS)


# ---------------------------------------------------------------------------
# 故障注入 —— 演练掉线与自愈的基础
# ---------------------------------------------------------------------------


def test_fail_mode_connect_refuses_connection():
    proto = _proto(sim_fail_mode="connect")
    with pytest.raises(ProtoConnectionError):
        proto.connect()
    assert proto.is_connected is False
    assert proto.health_check() is False


def test_fail_mode_read_connects_but_cannot_read():
    """「连得上却读不到」是现场很常见的一种,得能单独演练。"""
    proto = _proto(sim_fail_mode="read")
    assert proto.connect() is True
    with pytest.raises(ReadError):
        proto.read_points(POINTS)


def test_flaky_drops_the_link_so_the_worker_has_to_reconnect():
    """flaky 必须真的把连接断掉。

    只让读失败、连接一直挂着的话,worker 永远不会走重连分支 ——
    「掉线 → 告警 → 自动重连 → 告警清除」这一整圈就演示不出来。
    """
    proto = _proto(sim_fail_mode="flaky", sim_fail_rate=1.0)
    # 连接这一步也按概率失败,rate=1 时必然失败
    with pytest.raises(ProtoConnectionError):
        proto.connect()

    # 手动置为已连接,验证读失败时会把连接断掉
    proto.is_connected = True
    with pytest.raises(ReadError):
        proto.read_points(POINTS)
    assert proto.is_connected is False, "flaky 读失败后必须断开,否则不会触发重连"


def test_flaky_with_zero_rate_never_fails():
    proto = _proto(sim_fail_mode="flaky", sim_fail_rate=0.0)
    assert proto.connect() is True
    assert len(proto.read_points(POINTS)) == len(POINTS)


def test_zero_values_are_not_swallowed_by_falsy_defaults():
    """``config.get(k) or default`` 会把 0 当成没配 —— 于是「概率设 0」变成 0.3。

    这条是自己踩出来的:``sim_fail_rate=0`` 本该永不失败,结果按 0.3 在失败。
    幅值/基线/周期同理,设 0 都会被悄悄换掉。
    """
    proto = _proto(sim_fail_rate=0, sim_amplitude=0, sim_baseline=0, sim_latency_ms=0)
    assert proto.fail_rate == 0.0
    assert proto.amplitude == 0.0
    assert proto.baseline == 0.0
