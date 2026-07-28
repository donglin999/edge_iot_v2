"""Unit tests for the protocol-agnostic read-plan abstraction.

These tests intentionally avoid the Django ORM — they construct plain
duck-typed stand-ins for ``Device`` / ``Point`` so the suite stays fast and
focused on the planning logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pytest

from acquisition.services.read_plan import (
    PointMeta,
    ReadGroup,
    ReadPlanBuilder,
    Reading,
)


# ---------------------------------------------------------------------------
# Lightweight stand-ins for Device / Point
# ---------------------------------------------------------------------------


@dataclass
class _StubTemplate:
    data_type: str = "uint16"


@dataclass
class _StubPoint:
    code: str
    address: str
    extra: Dict[str, Any] = field(default_factory=dict)
    template: Optional[_StubTemplate] = None


@dataclass
class _StubDevice:
    protocol: str = "modbus_tcp"
    metadata: Dict[str, Any] = field(default_factory=lambda: {"slave_id": 1})


def _mk_points(addresses, fc: int = 3, data_type: str = "uint16") -> List[_StubPoint]:
    """Build N points sharing a function code + data type."""
    return [
        _StubPoint(
            code=f"P{i:02d}",
            address=str(addr),
            extra={"function_code": fc},
            template=_StubTemplate(data_type=data_type),
        )
        for i, addr in enumerate(addresses)
    ]


# ---------------------------------------------------------------------------
# Required cases
# ---------------------------------------------------------------------------


class TestModbusPlanCoalescing:
    """Exercises the tolerant-merge greedy algorithm in `_build_modbus_plan`."""

    def test_case1_gap_threshold_4_merges_into_one_group(self):
        """9 points with gaps <=4 collapse into a single 11-register read."""
        device = _StubDevice()
        addrs = [0, 1, 2, 4, 6, 7, 8, 9, 10]
        points = _mk_points(addrs)

        groups = ReadPlanBuilder.build(device, points, gap_threshold=4, max_registers=100)

        assert len(groups) == 1, f"expected 1 group, got {len(groups)}: {groups}"
        g = groups[0]
        assert g.function_code == 3
        assert g.start_address == 0
        # span: 10 + 1 register - 0 = 11
        assert g.register_count == 11
        assert len(g.points) == 9
        assert [p.code for p in g.points] == [f"P{i:02d}" for i in range(9)]
        assert g.extra["slave_id"] == 1

    def test_case2_gap_threshold_zero_splits_when_not_strictly_contiguous(self):
        """With gap_threshold=0, 0,1,2 / 4 / 6,7,8,9,10 split into 3 groups."""
        device = _StubDevice()
        addrs = [0, 1, 2, 4, 6, 7, 8, 9, 10]
        points = _mk_points(addrs)

        groups = ReadPlanBuilder.build(device, points, gap_threshold=0, max_registers=100)

        # Boundaries: gap from 2->4 = 1 (>0), gap from 4->6 = 1 (>0)
        assert len(groups) == 3, f"expected 3 groups, got {len(groups)}: {groups}"
        spans = [(g.start_address, g.register_count, len(g.points)) for g in groups]
        assert spans == [(0, 3, 3), (4, 1, 1), (6, 5, 5)]

    def test_case3_max_registers_caps_group_size(self):
        """A 100+ register span with max_registers=100 splits into multiple groups."""
        device = _StubDevice()
        # 0..150 step 10 — strictly contiguous after merge, but span >100
        addrs = list(range(0, 151, 10))  # 16 points, last addr=150
        points = _mk_points(addrs)

        # gap_threshold large enough that gap (=9) wouldn't break merging
        groups = ReadPlanBuilder.build(device, points, gap_threshold=10, max_registers=100)

        assert len(groups) >= 2, f"expected splitting, got {len(groups)} groups"
        # No group may exceed the cap
        for g in groups:
            assert g.register_count <= 100, f"group exceeds cap: {g}"
        # All points present, in order
        flat = [p.code for g in groups for p in g.points]
        assert flat == [f"P{i:02d}" for i in range(len(addrs))]

    def test_case4_different_function_codes_never_merge(self):
        """Mixing FC3 and FC4 always yields >=2 groups even if addresses align."""
        device = _StubDevice()
        fc3_pts = _mk_points([0, 1, 2], fc=3)
        fc4_pts = _mk_points([0, 1, 2], fc=4)
        # Re-code so codes are unique across the two groups
        for i, p in enumerate(fc4_pts):
            p.code = f"Q{i:02d}"

        groups = ReadPlanBuilder.build(
            device, fc3_pts + fc4_pts, gap_threshold=4, max_registers=100
        )

        fcs = sorted({g.function_code for g in groups})
        assert fcs == [3, 4], f"expected groups in both fc3 and fc4, got {fcs}"
        # No group should mix function codes
        for g in groups:
            assert all(p.function_code == g.function_code for p in g.points)

    def test_case5_empty_points_returns_empty_list(self):
        device = _StubDevice()
        assert ReadPlanBuilder.build(device, [], gap_threshold=4) == []


# ---------------------------------------------------------------------------
# Bonus coverage for the bits the pipeline relies on
# ---------------------------------------------------------------------------


class TestModbusPlanDetails:

    def test_float32_consumes_two_registers(self):
        device = _StubDevice()
        points = [
            _StubPoint("F1", "0", template=_StubTemplate("float32"), extra={"function_code": 3}),
            _StubPoint("F2", "2", template=_StubTemplate("float32"), extra={"function_code": 3}),
        ]
        groups = ReadPlanBuilder.build(device, points, gap_threshold=0)
        assert len(groups) == 1
        g = groups[0]
        assert g.start_address == 0
        # Two float32 = 4 registers, strictly contiguous
        assert g.register_count == 4
        assert all(p.num_registers == 2 for p in g.points)

    def test_int64_consumes_four_registers(self):
        device = _StubDevice()
        points = [
            _StubPoint("L1", "0", template=_StubTemplate("int64"), extra={"function_code": 3}),
        ]
        groups = ReadPlanBuilder.build(device, points)
        assert groups[0].register_count == 4
        assert groups[0].points[0].num_registers == 4

    def test_explicit_num_overrides_data_type_default(self):
        """``point.extra['num']`` should win over the data-type lookup."""
        device = _StubDevice()
        points = [
            _StubPoint(
                "X", "0",
                template=_StubTemplate("uint16"),
                extra={"function_code": 3, "num": 4},
            ),
        ]
        groups = ReadPlanBuilder.build(device, points)
        assert groups[0].points[0].num_registers == 4
        assert groups[0].register_count == 4

    def test_scada_address_is_parsed(self):
        """``D100`` -> 100 (0-based offset)."""
        device = _StubDevice()
        points = [
            _StubPoint("A", "D100", template=_StubTemplate("uint16"), extra={"function_code": 3}),
            _StubPoint("B", "D101", template=_StubTemplate("uint16"), extra={"function_code": 3}),
        ]
        groups = ReadPlanBuilder.build(device, points, gap_threshold=0)
        assert len(groups) == 1
        assert groups[0].start_address == 100
        assert groups[0].register_count == 2

    def test_default_function_code_is_3(self):
        """Points with no function_code in extra default to FC3 (holding)."""
        device = _StubDevice()
        points = [
            _StubPoint("A", "0", template=_StubTemplate("uint16")),
            _StubPoint("B", "1", template=_StubTemplate("uint16")),
        ]
        groups = ReadPlanBuilder.build(device, points)
        assert all(g.function_code == 3 for g in groups)

    def test_slave_id_propagated_from_metadata(self):
        device = _StubDevice(metadata={"slave_id": 7})
        points = _mk_points([0, 1])
        groups = ReadPlanBuilder.build(device, points)
        assert groups[0].extra["slave_id"] == 7


class TestDefaultPlan:
    """Non-Modbus pull protocols fall back to one-point-per-group.

    (mqtt/scada 是队列型推协议,整设备一组 —— 见 TestQueueDrainProtocolsGetOneGroup。)
    """

    def test_opcua_each_point_is_its_own_group(self):
        device = _StubDevice(protocol="opcua", metadata={})
        points = [
            _StubPoint("T1", "0", extra={"topic": "sensor/1"}),
            _StubPoint("T2", "0", extra={"topic": "sensor/2"}),
        ]
        groups = ReadPlanBuilder.build(device, points)
        assert len(groups) == 2
        assert all(len(g.points) == 1 for g in groups)
        assert all(g.function_code is None for g in groups)
        assert groups[0].points[0].extra.get("topic") == "sensor/1"


# ---------------------------------------------------------------------------
# Modbus.read_batch decode + error handling
# ---------------------------------------------------------------------------


class _FakeMaster:
    """Stand-in for modbus_tk's TcpMaster, returning canned register data."""

    def __init__(self, payload, *, fail: bool = False) -> None:
        self.payload = payload
        self.fail = fail
        self.calls: List[Dict[str, Any]] = []

    def execute(self, *, slave, function_code, starting_address, quantity_of_x):
        self.calls.append(
            {
                "slave": slave,
                "function_code": function_code,
                "starting_address": starting_address,
                "quantity_of_x": quantity_of_x,
            }
        )
        if self.fail:
            raise RuntimeError("simulated transport failure")
        return tuple(self.payload[:quantity_of_x])


class TestModbusReadBatch:

    def _make_protocol(self, payload, *, fail: bool = False, byte_order: str = "big"):
        from acquisition.protocols.modbus import ModbusTCPProtocol

        proto = ModbusTCPProtocol(
            {
                "source_ip": "127.0.0.1",
                "source_port": 5020,
                "slave_id": 1,
                "byte_order": byte_order,
                "timeout": 1.0,
            }
        )
        proto.master = _FakeMaster(payload, fail=fail)
        proto.is_connected = True
        return proto

    def test_decodes_uint16_points_in_one_group(self):
        proto = self._make_protocol(payload=[10, 20, 30])
        group = ReadGroup(
            protocol_type="modbus_tcp",
            points=[
                PointMeta("A", 0, 1, "uint16", function_code=3),
                PointMeta("B", 1, 1, "uint16", function_code=3),
                PointMeta("C", 2, 1, "uint16", function_code=3),
            ],
            function_code=3,
            start_address=0,
            register_count=3,
        )
        readings = proto.read_batch(group)
        assert [r.point_code for r in readings] == ["A", "B", "C"]
        assert [r.value for r in readings] == [10, 20, 30]
        assert all(r.quality == "good" for r in readings)
        assert all(r.timestamp_ns > 0 for r in readings)
        assert proto.master.calls[0]["starting_address"] == 0
        assert proto.master.calls[0]["quantity_of_x"] == 3

    def test_transport_failure_raises_read_error(self):
        from acquisition.protocols.base import ReadError

        proto = self._make_protocol(payload=[], fail=True)
        group = ReadGroup(
            protocol_type="modbus_tcp",
            points=[PointMeta("A", 0, 1, "uint16", function_code=3)],
            function_code=3, start_address=0, register_count=1,
        )
        with pytest.raises(ReadError):
            proto.read_batch(group)

    def test_out_of_range_point_marked_bad_others_succeed(self):
        # 2 registers returned, but second point asks for an offset beyond data
        proto = self._make_protocol(payload=[5, 9])
        group = ReadGroup(
            protocol_type="modbus_tcp",
            points=[
                PointMeta("A", 0, 1, "uint16", function_code=3),
                PointMeta("Z", 5, 1, "uint16", function_code=3),  # out of range
            ],
            function_code=3, start_address=0, register_count=2,
        )
        readings = proto.read_batch(group)
        codes = {r.point_code: r for r in readings}
        assert codes["A"].quality == "good"
        assert codes["A"].value == 5
        assert codes["Z"].quality == "bad"
        assert codes["Z"].value is None


# ===========================================================================
# 字符串地址不能被 int() 抹掉(S7 / OPC-UA)
# ===========================================================================


class _StrAddrPoint:
    """带字符串地址的测点 —— S7 的 DB1.DBD0、OPC-UA 的 ns=2;s=Tag1。"""

    def __init__(self, code, address, extra=None):
        self.code = code
        self.address = address
        self.extra = extra or {}
        self.template = None


class TestStringAddressSurvivesThePlan:
    """``PointMeta.address`` 是 Modbus 语义的整数线址,这些协议根本没有整数地址。

    之前 ``_build_default_plan`` 直接 ``int(address)``,失败就落 0,原始地址就此
    丢失:S7 每个测点都报「无法解析 S7 地址 '0'」,OPC-UA 去读一个叫 "0" 的节点。
    两个协议在流水线里等于完全不工作 —— 而单测里手搓 point dict 的用例发现不了,
    因为它们绕开了读计划。
    """

    def _plan(self, protocol, address, extra=None):
        from types import SimpleNamespace

        device = SimpleNamespace(protocol=protocol, metadata={})
        groups = ReadPlanBuilder.build(
            device, [_StrAddrPoint("pt", address, extra)]
        )
        return groups[0].points[0]

    def test_s7_db_address_is_preserved(self):
        meta = self._plan("siemens_s7", "DB1.DBD0", {"data_type": "float32"})
        assert meta.extra["address"] == "DB1.DBD0"

    def test_opcua_node_id_is_preserved(self):
        meta = self._plan("opcua", "ns=2;s=Channel1.Device1.Tag1")
        assert meta.extra["address"] == "ns=2;s=Channel1.Device1.Tag1"

    def test_read_batch_hands_the_string_address_to_the_protocol(self):
        """真正的验收点:协议 read_points 收到的 dict 里必须是原始地址。"""
        from acquisition.protocols.base import BaseProtocol, ProtocolMeta

        seen = {}

        class _Spy(BaseProtocol):
            META = ProtocolMeta(name="spy", label="Spy", category="other")

            def connect(self): return True
            def disconnect(self): return None
            def health_check(self): return True
            def read_points(self, points):
                seen["points"] = points
                return [{"code": p["code"], "value": 1, "quality": "good"} for p in points]

        from types import SimpleNamespace

        device = SimpleNamespace(protocol="siemens_s7", metadata={})
        groups = ReadPlanBuilder.build(
            device, [_StrAddrPoint("motor", "DB1.DBD0", {"data_type": "float32"})]
        )
        _Spy({}).read_batch(groups[0])
        assert seen["points"][0]["address"] == "DB1.DBD0"

    def test_numeric_address_still_becomes_an_int(self):
        """别把 Modbus 的整数线址一起改坏。"""
        meta = self._plan("mqtt", "40001")
        assert meta.address == 40001


class TestQueueDrainProtocolsGetOneGroup:
    """队列型推协议(mqtt/scada)必须整设备一个读组。

    这些协议的 read_points = 「抽干共享接收队列 + 按传入测点集合匹配」。按点
    拆组时,第一组的 drain 清空队列却只保留自己那个测点的消息,其余测点的数据
    全被扔掉;后续各组再各自空等 read_timeout。中山博创现场 510 测点实测:
    MQTT 推送速率远高于入库速率(0.4 点/秒),丢的就是这里 —— 本地 2 测点的
    mock 症状轻微,从未暴露。
    """

    def _plan(self, protocol, n=5):
        from types import SimpleNamespace

        device = SimpleNamespace(protocol=protocol, metadata={})
        return ReadPlanBuilder.build(
            device, [_StrAddrPoint(f"P{i:03d}", f"P{i:03d}") for i in range(n)]
        )

    def test_scada_all_points_collapse_into_one_group(self):
        groups = self._plan("scada", n=510)
        assert len(groups) == 1
        assert len(groups[0].points) == 510
        assert {m.code for m in groups[0].points} == {f"P{i:03d}" for i in range(510)}

    def test_mqtt_all_points_collapse_into_one_group(self):
        groups = self._plan("mqtt", n=7)
        assert len(groups) == 1
        assert len(groups[0].points) == 7

    def test_pull_protocols_keep_per_point_groups(self):
        """OPC-UA/S7 是请求-响应式,每点一组的既有行为不能被改坏。"""
        assert len(self._plan("opcua", n=5)) == 5
        assert len(self._plan("siemens_s7", n=5)) == 5

    def test_single_point_stays_single_group(self):
        assert len(self._plan("scada", n=1)) == 1


class TestQueueDrainEndToEnd:
    """验收点:scada 单周期 read_batch 消费队列里**所有**测点的消息。"""

    def test_one_cycle_consumes_messages_for_every_point(self):
        import json
        from types import SimpleNamespace

        from acquisition.protocols.scada import SCADAProtocol

        proto = SCADAProtocol({
            "source_ip": "127.0.0.1", "source_port": 1883,
            "scada_product_key": "pk1", "scada_device_name": "DEV1",
        })
        proto.is_connected = True  # 不真连,直接喂队列

        codes = [f"P{i:03d}" for i in range(6)]
        for i, code in enumerate(codes):
            proto.data_queue.put({
                "topic": f"/sys/pk1/device/DEV1/thing/property/{code}/post",
                "payload": json.dumps({"data": {
                    "propertyCode": code, "propertyValue": float(i),
                    "time": "1755653755532",
                }}),
                "timestamp": 1_755_653_755_532_000_000,
                "qos": 0,
            })

        device = SimpleNamespace(protocol="scada", metadata={})
        groups = ReadPlanBuilder.build(
            device, [_StrAddrPoint(c, c) for c in codes]
        )
        assert len(groups) == 1

        readings = proto.read_batch(groups[0])
        got = {r.point_code: r.value for r in readings}
        assert got == {c: float(i) for i, c in enumerate(codes)}
