"""Agent A · Modbus 全数据类型 + 全功能码 + 全字节序 —— 真实 TCP round-trip 测试。

覆盖 team-prompts/2026-07-23-A-modbus-full-datatypes.md 任务书的 5 项:
  1. modbus.py `_DATA_TYPES` 全部类型,含负数/边界值
  2. 功能码 1(线圈)/2(离散输入)/3(保持)/4(输入寄存器)
  3. 字节序 big/little/big-swap/little-swap(float32/int32 各验证一轮)
  4. 多点批量合并读 —— 混合数据类型下寄存器偏移正确(float32 后紧跟 uint16 不串位)
  5. `_combine_registers` 参数化(RTU 与 TCP 共用同一解码路径,不需要真串口)

范式:mock 的是「对端设备」—— 真 modbus_tk TcpServer(ModbusMockServer),协议层
用真实 ModbusTCPProtocol 做真 I/O。`encode_scalar`(mock 侧)与 `_combine_registers`
(协议解码侧)是两套独立实现,不共享字节序逻辑,避免对称 bug 互相抵消掉。
"""
from __future__ import annotations

import socket
import time
from contextlib import closing
from typing import Optional

import pytest

from acquisition.protocols.modbus import (
    ModbusTCPProtocol,
    _LegacyDevice,
    _LegacyPoint,
    _combine_registers,
)
from acquisition.services.read_plan import ReadPlanBuilder
from acquisition.testing.modbus_mock_server import ModbusMockServer, encode_scalar

PORT_RANGE = range(15100, 15200)


def _free_port() -> int:
    for port in PORT_RANGE:
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("no free port in 15100-15199")


@pytest.fixture()
def server():
    """Start a real ModbusMockServer on a free port in our range; always stop it."""
    srv: Optional[ModbusMockServer] = None
    last_exc: Optional[Exception] = None
    for _ in range(5):
        port = _free_port()
        candidate = ModbusMockServer(host="127.0.0.1", port=port, slave_ids=[1])
        try:
            candidate.start()
            srv = candidate
            break
        except OSError as exc:  # port raced by something else — retry another
            last_exc = exc
            time.sleep(0.05)
    if srv is None:
        raise RuntimeError(f"failed to start mock modbus server in 15100-15199: {last_exc}")
    try:
        yield srv
    finally:
        srv.stop()


def _connected_protocol(srv: ModbusMockServer, byte_order: str = "big") -> ModbusTCPProtocol:
    proto = ModbusTCPProtocol({
        "source_ip": srv.host,
        "source_port": srv.port,
        "slave_id": 1,
        "byte_order": byte_order,
        "timeout": 3.0,
    })
    assert proto.connect() is True
    return proto


# ---------------------------------------------------------------------------
# 1) 全部数据类型,含负数 / 边界值 —— 通过真实 TCP round-trip
# ---------------------------------------------------------------------------

DATA_TYPE_CASES = [
    ("uint16", 0), ("uint16", 65535), ("uint16", 12345),
    ("int16", 0), ("int16", -1), ("int16", -32768), ("int16", 32767),
    ("uint32", 0), ("uint32", 4294967295), ("uint32", 305419896),
    ("int32", 0), ("int32", -1), ("int32", -2147483648), ("int32", 2147483647),
    ("uint64", 0), ("uint64", 18446744073709551615), ("uint64", 1234567890123456789),
    ("int64", 0), ("int64", -1), ("int64", -9223372036854775808), ("int64", 9223372036854775807),
    ("float32", 0.0), ("float32", -1.5), ("float32", 3.14159), ("float32", 1e30), ("float32", -1e30),
    ("float64", 0.0), ("float64", -123456.789), ("float64", 1e150), ("float64", -1e-150),
    ("bool", True), ("bool", False),
]


@pytest.mark.parametrize("data_type,value", DATA_TYPE_CASES)
def test_data_type_roundtrip(server, data_type, value):
    server.write_value(3, 40, value, data_type, byte_order="big", slave_id=1)
    proto = _connected_protocol(server)
    try:
        result = proto.read_points([
            {"code": "p", "address": "40", "function_code": 3, "data_type": data_type}
        ])
    finally:
        proto.disconnect()

    assert len(result) == 1
    assert result[0]["quality"] == "good", result[0]
    got = result[0]["value"]
    if data_type == "float32":
        assert got == pytest.approx(value, rel=1e-6)
    elif data_type == "float64":
        assert got == pytest.approx(value, rel=1e-12)
    else:
        assert got == value


def test_int16_negative_regression(server):
    """Regression for the sign-extension bug found in _combine_registers:

    modbus-tk decodes every register with struct format '>H' (unsigned), so
    an int16 value of -1 arrives on the wire as the bit pattern 0xFFFF /
    65535. Before the fix, `_combine_registers([65535], "int16", 1, "big")`
    returned 65535 instead of -1 because the num<=1 fast path never sign-
    extended int16 (only bool got special treatment). Location:
    acquisition/protocols/modbus.py::_combine_registers.
    """
    server.write_value(3, 40, -1, "int16", slave_id=1)
    proto = _connected_protocol(server)
    try:
        result = proto.read_points([
            {"code": "p", "address": "40", "function_code": 3, "data_type": "int16"}
        ])
    finally:
        proto.disconnect()
    assert result[0]["value"] == -1


# ---------------------------------------------------------------------------
# 2) 全部功能码:1(线圈)/2(离散输入)/3(保持)/4(输入寄存器)
# ---------------------------------------------------------------------------

FUNCTION_CODE_CASES = [
    (1, "bool", True),
    (1, "bool", False),
    (2, "bool", True),
    (2, "bool", False),
    (3, "uint16", 4321),
    (4, "uint16", 4321),
    (3, "int32", -123456),
    (4, "int32", -123456),
    (3, "float32", -9.5),
    (4, "float32", -9.5),
]


@pytest.mark.parametrize("function_code,data_type,value", FUNCTION_CODE_CASES)
def test_function_codes(server, function_code, data_type, value):
    server.write_value(function_code, 20, value, data_type, slave_id=1)
    proto = _connected_protocol(server)
    try:
        result = proto.read_points([
            {"code": "p", "address": "20", "function_code": function_code, "data_type": data_type}
        ])
    finally:
        proto.disconnect()

    assert result[0]["quality"] == "good", result[0]
    got = result[0]["value"]
    if data_type == "float32":
        assert got == pytest.approx(value, rel=1e-6)
    else:
        assert got == value


# ---------------------------------------------------------------------------
# 3) 全部字节序:big / little / big-swap / little-swap(float32、int32 各一轮)
# ---------------------------------------------------------------------------

BYTE_ORDERS = ("big", "little", "big-swap", "little-swap")


@pytest.mark.parametrize("byte_order", BYTE_ORDERS)
@pytest.mark.parametrize("data_type,value", [
    ("float32", -98765.4375),
    ("float32", 0.000125),
    ("int32", -305419896),
    ("int32", 2018915346),
])
def test_byte_order_roundtrip(server, byte_order, data_type, value):
    server.write_value(3, 40, value, data_type, byte_order=byte_order, slave_id=1)
    proto = _connected_protocol(server, byte_order=byte_order)
    try:
        result = proto.read_points([
            {"code": "p", "address": "40", "function_code": 3, "data_type": data_type}
        ])
    finally:
        proto.disconnect()

    got = result[0]["value"]
    assert result[0]["quality"] == "good", result[0]
    if data_type == "float32":
        assert got == pytest.approx(value, rel=1e-6)
    else:
        assert got == value


def test_byte_order_mismatch_actually_matters(server):
    """Sanity check that the byte-order plumbing isn't a no-op: writing
    little-swap and reading back as big must NOT reproduce the original
    value (otherwise the whole byte_order test suite above would be
    vacuously true)."""
    value = -305419896
    server.write_value(3, 40, value, "int32", byte_order="little-swap", slave_id=1)
    proto = _connected_protocol(server, byte_order="big")
    try:
        result = proto.read_points([
            {"code": "p", "address": "40", "function_code": 3, "data_type": "int32"}
        ])
    finally:
        proto.disconnect()
    assert result[0]["value"] != value


# ---------------------------------------------------------------------------
# 4) 多点批量合并读:混合数据类型下寄存器偏移正确,float32 后紧跟 uint16 不串位
# ---------------------------------------------------------------------------

def test_multi_point_batch_merge_mixed_types(server):
    # code, address, data_type, register width, value
    layout = [
        ("u1", 40, "uint16", 1, 111),
        ("f1", 41, "float32", 2, -2.5),
        ("u2", 43, "uint16", 1, 222),
        ("i1", 44, "int16", 1, -333),
        ("f2", 45, "float64", 4, 123456.789),
        ("u3", 49, "uint16", 1, 333),
    ]
    for code, addr, dt, num, val in layout:
        server.write_value(3, addr, val, dt, slave_id=1)

    points = [
        {"code": code, "address": str(addr), "function_code": 3, "data_type": dt}
        for code, addr, dt, num, val in layout
    ]

    # Assert the plan builder actually merges this into ONE contiguous group
    # (that's the scenario this test exists to exercise) rather than silently
    # falling back to one-group-per-point, which would make offset bugs
    # invisible.
    groups = ReadPlanBuilder.build(
        _LegacyDevice("modbus_tcp", 1), [_LegacyPoint(p) for p in points]
    )
    assert len(groups) == 1, f"expected a single merged group, got {len(groups)}"
    last_addr, last_dt, last_num, _ = 49, "uint16", 1, 333
    assert groups[0].register_count == (last_addr + last_num) - 40
    assert groups[0].function_code == 3
    assert groups[0].start_address == 40

    proto = _connected_protocol(server)
    try:
        result = proto.read_points(points)
    finally:
        proto.disconnect()

    by_code = {r["code"]: r for r in result}
    assert set(by_code) == {c for c, *_ in layout}
    for code, addr, dt, num, val in layout:
        row = by_code[code]
        assert row["quality"] == "good", (code, row)
        if dt.startswith("float"):
            rel = 1e-6 if dt == "float32" else 1e-9
            assert row["value"] == pytest.approx(val, rel=rel), code
        else:
            assert row["value"] == val, code


# ---------------------------------------------------------------------------
# 5) _combine_registers 参数化(RTU 与 TCP 共用同一解码路径)
# ---------------------------------------------------------------------------

COMBINE_REGISTERS_CASES = [
    ("uint16", 0, 1), ("uint16", 65535, 1), ("uint16", 1, 1),
    ("int16", 0, 1), ("int16", -1, 1), ("int16", -32768, 1), ("int16", 32767, 1),
    ("bool", True, 1), ("bool", False, 1),
    ("uint32", 0, 2), ("uint32", 4294967295, 2), ("uint32", 305419896, 2),
    ("int32", 0, 2), ("int32", -1, 2), ("int32", -2147483648, 2), ("int32", 2147483647, 2),
    ("float32", 0.0, 2), ("float32", -1.5, 2), ("float32", 123456.75, 2),
    ("uint64", 0, 4), ("uint64", 18446744073709551615, 4),
    ("int64", 0, 4), ("int64", -1, 4), ("int64", -9223372036854775808, 4), ("int64", 9223372036854775807, 4),
    ("float64", 0.0, 4), ("float64", -1.23456789e10, 4), ("float64", 5e-100, 4),
]


@pytest.mark.parametrize("byte_order", BYTE_ORDERS)
@pytest.mark.parametrize("data_type,value,num", COMBINE_REGISTERS_CASES)
def test_combine_registers_direct(byte_order, data_type, value, num):
    """Direct unit coverage of the RTU/TCP-shared decode path — no socket
    needed, this is exactly what ModbusRTUProtocol.read_batch also calls."""
    registers = encode_scalar(value, data_type, byte_order)
    assert len(registers) == num
    got = _combine_registers(registers, data_type, num, byte_order)
    if data_type == "float32":
        assert got == pytest.approx(value, rel=1e-6)
    elif data_type == "float64":
        assert got == pytest.approx(value, rel=1e-12)
    else:
        assert got == value


def test_combine_registers_empty_returns_zero():
    assert _combine_registers([], "uint16", 1, "big") == 0
