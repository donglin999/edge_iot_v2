"""OPC-UA 真服务器联调测试。

范式:被采对象是**真的 OPC-UA 服务器**(python-opcua 的 ``Server``,见
``acquisition/testing/opcua_mock_server.py``),采集走的是真正的 ``OPCUAProtocol``
(asyncua 客户端)—— 真开 TCP、真建会话、真按 NodeId 读值。不用 simulator/假客户端,
这样才能查出「NodeId 解析错」「类型没对上」「一个坏点污染整批」这类只在真实
OPC-UA 会话路径才暴露的问题。

端口段:15300-15399,固定从 15340 起,冲突则在段内换端口重试。
"""
from __future__ import annotations

import socket
import time

import pytest

# 真服务器测试需要 asyncua(协议客户端)与 opcua(服务器)。本机 asyncua 只装在
# Homebrew python3.11 —— 系统 3.9 跑全量套件时整个文件跳过,不许 import 失败炸掉收集。
pytest.importorskip("asyncua", reason="asyncua 未安装(装在 python3.11),跳过 OPC-UA 真服务器测试")
pytest.importorskip("opcua", reason="python-opcua 未安装,无法起 OPC-UA mock 服务器")

from acquisition.protocols.base import ConnectionError as ProtoConnectionError
from acquisition.protocols.base import ReadError
from acquisition.protocols.opcua import OPCUAProtocol
from acquisition.testing.opcua_mock_server import OPCUAMockServer

PORT_RANGE = range(15340, 15400)


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) != 0


def _start_server_with_retry() -> OPCUAMockServer:
    """在分配段内找一个空闲端口起服务器;冲突则换下一个端口重试。"""
    last_exc = None
    for port in PORT_RANGE:
        if not _port_free(port):
            continue
        srv = OPCUAMockServer(port=port)
        try:
            srv.start()
            # 给 socket 一点时间就绪。
            time.sleep(0.6)
            return srv
        except OSError as exc:  # 端口刚好被抢占,换下一个
            last_exc = exc
            try:
                srv.stop()
            except Exception:  # noqa: BLE001
                pass
            continue
    raise RuntimeError(f"15340-15399 段内没有可用端口: {last_exc}")


@pytest.fixture()
def opcua_server():
    """真 OPC-UA 服务器,函数级别起停,保证不留孤儿进程/线程。"""
    srv = _start_server_with_retry()
    try:
        yield srv
    finally:
        srv.stop()


def _device_config(srv: OPCUAMockServer, **overrides) -> dict:
    cfg = {"endpoint_url": srv.endpoint_url, "security_policy": "None", "timeout": 5.0}
    cfg.update(overrides)
    return cfg


def _all_points():
    return [{"code": p["code"], "address": p["address"]} for p in OPCUAMockServer.POINTS]


# --------------------------------------------------------------------- 1/2: 全类型 + 两种 NodeId 形态
class TestAllDataTypesRealServer:
    """真服务器起多类型节点,真 asyncua 客户端连上读,断言类型与值都对。"""

    def test_read_all_points_good_quality(self, opcua_server):
        proto = OPCUAProtocol(_device_config(opcua_server))
        assert proto.connect() is True
        try:
            results = proto.read_points(_all_points())
        finally:
            proto.disconnect()

        assert len(results) == len(OPCUAMockServer.POINTS)
        by_code = {r["code"]: r for r in results}
        for spec in OPCUAMockServer.POINTS:
            row = by_code[spec["code"]]
            assert row["quality"] == "good", f"{spec['code']} 应为 good, 实际 {row}"
            assert row["value"] is not None

    def test_double_and_float_are_numeric_in_range(self, opcua_server):
        proto = OPCUAProtocol(_device_config(opcua_server))
        proto.connect()
        try:
            results = proto.read_points([
                {"code": "d_double", "address": "ns=2;i=1001"},
                {"code": "d_float", "address": "ns=2;i=1002"},
            ])
        finally:
            proto.disconnect()
        by_code = {r["code"]: r for r in results}
        assert isinstance(by_code["d_double"]["value"], float)
        assert -50.0 <= by_code["d_double"]["value"] <= 50.0
        assert isinstance(by_code["d_float"]["value"], float)
        assert 0.0 <= by_code["d_float"]["value"] <= 100.0

    def test_int_family_types_and_ranges(self, opcua_server):
        proto = OPCUAProtocol(_device_config(opcua_server))
        proto.connect()
        try:
            results = proto.read_points([
                {"code": "d_int16", "address": "ns=2;s=Channel1.Device1.Int16Tag"},
                {"code": "d_int32", "address": "ns=2;i=1004"},
                {"code": "d_int64", "address": "ns=2;s=Channel1.Device1.Int64Tag"},
                {"code": "d_uint16", "address": "ns=2;i=1006"},
                {"code": "d_uint32", "address": "ns=2;s=Channel1.Device1.UInt32Tag"},
            ])
        finally:
            proto.disconnect()
        by_code = {r["code"]: r for r in results}
        assert isinstance(by_code["d_int16"]["value"], int)
        assert -32768 <= by_code["d_int16"]["value"] <= 32767
        assert isinstance(by_code["d_int32"]["value"], int)
        assert by_code["d_int32"]["value"] >= 1
        assert isinstance(by_code["d_int64"]["value"], int)
        assert by_code["d_int64"]["value"] >= 10_000_000_000
        assert isinstance(by_code["d_uint16"]["value"], int)
        assert 0 <= by_code["d_uint16"]["value"] <= 65535
        assert isinstance(by_code["d_uint32"]["value"], int)
        assert 0 <= by_code["d_uint32"]["value"] <= 4_294_967_295

    def test_bool_and_string_types(self, opcua_server):
        proto = OPCUAProtocol(_device_config(opcua_server))
        proto.connect()
        try:
            results = proto.read_points([
                {"code": "d_bool", "address": "ns=2;i=1008"},
                {"code": "d_string", "address": "ns=2;s=Channel1.Device1.StringTag"},
            ])
        finally:
            proto.disconnect()
        by_code = {r["code"]: r for r in results}
        assert isinstance(by_code["d_bool"]["value"], bool)
        assert isinstance(by_code["d_string"]["value"], str)
        assert by_code["d_string"]["value"].startswith("mock-")

    def test_values_change_over_time(self, opcua_server):
        """跨两次读取,至少有一个随时间变化的点数值不同 —— 不是死值。"""
        proto = OPCUAProtocol(_device_config(opcua_server))
        proto.connect()
        try:
            points = [{"code": "d_int32", "address": "ns=2;i=1004"}]
            first = proto.read_points(points)[0]["value"]
            time.sleep(1.0)
            second = proto.read_points(points)[0]["value"]
        finally:
            proto.disconnect()
        assert second > first  # d_int32 是单调递增计数器

    def test_nodeid_numeric_and_string_forms_both_resolve(self, opcua_server):
        """NodeId 覆盖 ns=X;i=数字 与 ns=X;s=字符串 两种形态,都要能读到。"""
        numeric_points = [p for p in OPCUAMockServer.POINTS if ";i=" in p["address"]]
        string_points = [p for p in OPCUAMockServer.POINTS if ";s=" in p["address"]]
        assert numeric_points, "测点集里应包含 ns=X;i=数字 形态"
        assert string_points, "测点集里应包含 ns=X;s=字符串 形态"

        proto = OPCUAProtocol(_device_config(opcua_server))
        proto.connect()
        try:
            results = proto.read_points(_all_points())
        finally:
            proto.disconnect()
        by_code = {r["code"]: r for r in results}
        for p in numeric_points + string_points:
            assert by_code[p["code"]]["quality"] == "good"


# --------------------------------------------------------------------- 3: 坏点隔离
class TestBadNodeIsolation:
    def test_one_bad_nodeid_among_batch_only_that_point_is_bad(self, opcua_server):
        points = _all_points() + [{"code": "ghost", "address": "ns=2;i=999999"}]
        proto = OPCUAProtocol(_device_config(opcua_server))
        proto.connect()
        try:
            results = proto.read_points(points)
        finally:
            proto.disconnect()

        by_code = {r["code"]: r for r in results}
        assert by_code["ghost"]["quality"] == "bad"
        assert by_code["ghost"]["value"] is None
        for spec in OPCUAMockServer.POINTS:
            assert by_code[spec["code"]]["quality"] == "good", (
                f"{spec['code']} 不应被 ghost 节点拖累"
            )

    def test_bad_string_nodeid_among_batch_only_that_point_is_bad(self, opcua_server):
        """坏点也可能是字符串形态的 NodeId(不存在的 ns=X;s=...)。"""
        points = _all_points() + [
            {"code": "ghost_str", "address": "ns=2;s=Does.Not.Exist"},
        ]
        proto = OPCUAProtocol(_device_config(opcua_server))
        proto.connect()
        try:
            results = proto.read_points(points)
        finally:
            proto.disconnect()
        by_code = {r["code"]: r for r in results}
        assert by_code["ghost_str"]["quality"] == "bad"
        good_count = sum(1 for r in results if r["quality"] == "good")
        assert good_count == len(OPCUAMockServer.POINTS)

    def test_all_bad_raises_read_error(self, opcua_server):
        """全部点都是坏 NodeId 时,read_points 应整体抛 ReadError(交易层失败,
        不是把一批全 bad 的结果悄悄放行)—— 和协议里其余分支的约定一致。"""
        proto = OPCUAProtocol(_device_config(opcua_server))
        proto.connect()
        try:
            with pytest.raises(ReadError):
                proto.read_points([
                    {"code": "ghost1", "address": "ns=2;i=888888"},
                    {"code": "ghost2", "address": "ns=2;i=888889"},
                ])
        finally:
            proto.disconnect()


# --------------------------------------------------------------------- 4: 断线重连
class TestReconnect:
    def test_read_fails_after_server_stop_but_connection_state_reported_correctly(self, opcua_server):
        proto = OPCUAProtocol(_device_config(opcua_server))
        proto.connect()
        assert proto.is_connected is True

        # 初次读一次,确认工作正常。
        proto.read_points(_all_points())

        # 服务器停掉,模拟断线。
        opcua_server.stop()

        with pytest.raises(ReadError):
            proto.read_points(_all_points())

        # disconnect() 之后连接态必须如实反映为 False(供上层 pipeline 判断是否需要重连)。
        proto.disconnect()
        assert proto.is_connected is False
        assert proto.health_check() is False

    def test_reconnect_after_server_restart_resumes_reading(self, opcua_server):
        port = opcua_server.port
        proto = OPCUAProtocol(_device_config(opcua_server))
        proto.connect()
        proto.read_points(_all_points())

        opcua_server.stop()
        try:
            proto.read_points(_all_points())
        except ReadError:
            pass
        proto.disconnect()

        # 在同一端口上重启服务器(端口段是我们独占的,重启复用同一端口安全)。
        restarted = OPCUAMockServer(port=port)
        try:
            restarted.start()
            time.sleep(0.6)
            assert proto.connect() is True
            results = proto.read_points(_all_points())
            assert all(r["quality"] == "good" for r in results)
        finally:
            proto.disconnect()
            restarted.stop()

    def test_connect_to_dead_endpoint_raises_connection_error(self, opcua_server):
        """服务器地址压根没监听时,connect() 必须报连接错误而不是挂起/静默成功。"""
        dead_port = opcua_server.port  # 先拿到端口号
        opcua_server.stop()
        time.sleep(0.2)
        proto = OPCUAProtocol(_device_config(opcua_server, timeout=2.0))
        with pytest.raises(ProtoConnectionError):
            proto.connect()
        assert proto.is_connected is False


# --------------------------------------------------------------------- 5: security_policy='None'
class TestSecurityPolicyNone:
    def test_connect_with_explicit_none_policy_succeeds(self, opcua_server):
        proto = OPCUAProtocol(_device_config(opcua_server, security_policy="None"))
        assert proto.connect() is True
        try:
            results = proto.read_points([{"code": "d_double", "address": "ns=2;i=1001"}])
            assert results[0]["quality"] == "good"
        finally:
            proto.disconnect()

    def test_connect_without_security_policy_key_defaults_to_none_and_succeeds(self, opcua_server):
        """设备配置里干脆不带 security_policy 字段,协议侧应默认 None 并正常连上
        (对应 DEVICE_FIELDS 里 security_policy 的 default="None")。"""
        cfg = {"endpoint_url": opcua_server.endpoint_url, "timeout": 5.0}
        proto = OPCUAProtocol(cfg)
        assert proto.security == "None"
        assert proto.connect() is True
        proto.disconnect()
