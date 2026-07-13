"""Regression tests for :meth:`BaseProtocol.read_batch` default implementation.

Bug: only ``_ModbusBase`` overrode ``read_batch``. Every other real protocol
(MQTT, Siemens S7, OPC-UA, Mitsubishi PLC) only implements ``read_points`` and
inherited ``BaseProtocol.read_batch``, which used to ``raise NotImplementedError``.
The continuous-acquisition pipeline (``ReadWorker._read_cycle``) calls
``read_batch`` every cycle, so those protocols connected and then produced ZERO
data forever — every cycle raised, was caught, and recorded as a failure.

These tests exercise the **real** protocol classes (not the ``tests/mocks``
stand-ins, which implement ``read_batch`` and therefore hid the bug). They fail
against the old ``NotImplementedError`` base and pass once the default maps
``read_points`` output onto the :class:`Reading` contract.
"""
from __future__ import annotations

import pytest

from acquisition.protocols.base import BaseProtocol
from acquisition.protocols.mqtt import MQTTProtocol
from acquisition.protocols.opcua import OPCUAProtocol
from acquisition.protocols.plc import MitsubishiPLCProtocol
from acquisition.protocols.s7 import SiemensS7Protocol
from acquisition.services.read_plan import PointMeta, ReadGroup, Reading

# Every real non-Modbus protocol, with a minimal device config each can be
# constructed from without any I/O at __init__ time.
_REAL_PROTOCOLS = [
    (MQTTProtocol, {"source_ip": "broker", "source_port": 1883, "mqtt_topics": "t"}),
    (SiemensS7Protocol, {"source_ip": "10.0.0.1", "rack": 0, "slot": 1}),
    (OPCUAProtocol, {"endpoint_url": "opc.tcp://10.0.0.1:4840"}),
    (MitsubishiPLCProtocol, {"source_ip": "10.0.0.1", "source_port": 6000}),
]


def _group(*codes: str) -> ReadGroup:
    """A non-Modbus read group (function_code/start_address left None)."""
    return ReadGroup(
        protocol_type="test",
        points=[
            PointMeta(code=c, address=i, num_registers=1, data_type="uint16")
            for i, c in enumerate(codes)
        ],
    )


@pytest.mark.parametrize("proto_cls,cfg", _REAL_PROTOCOLS)
def test_real_protocols_inherit_default_read_batch(proto_cls, cfg):
    """The real non-Modbus protocols rely on the inherited default (no override)."""
    assert proto_cls.read_batch is BaseProtocol.read_batch


@pytest.mark.parametrize("proto_cls,cfg", _REAL_PROTOCOLS)
def test_default_read_batch_does_not_raise_notimplemented(proto_cls, cfg, monkeypatch):
    """read_batch must aggregate read_points output, not raise NotImplementedError.

    ``read_points`` (the collaborator each real protocol *does* implement) is
    stubbed to stand in for a successful transport read, so the assertion is
    purely about the inherited ``read_batch`` mapping.
    """
    proto = proto_cls(cfg)
    proto.is_connected = True

    captured = {}

    def fake_read_points(points):
        captured["points"] = points
        return [
            {"code": "A", "value": 11, "timestamp": 111, "quality": "good"},
            {"code": "B", "value": 22, "timestamp": 222, "quality": "good"},
        ]

    monkeypatch.setattr(proto, "read_points", fake_read_points)

    readings = proto.read_batch(_group("A", "B"))

    # Correct aggregated shape: a list of Reading objects mapped 1:1.
    assert all(isinstance(r, Reading) for r in readings)
    assert [(r.point_code, r.value, r.timestamp_ns, r.quality) for r in readings] == [
        ("A", 11, 111, "good"),
        ("B", 22, 222, "good"),
    ]

    # read_batch projected the group's PointMeta into the dict shape
    # read_points consumes (code + address + type hints carried through).
    assert {p["code"] for p in captured["points"]} == {"A", "B"}
    for p in captured["points"]:
        assert "address" in p and "data_type" in p


def test_default_read_batch_partial_failure_degrades_gracefully(monkeypatch):
    """A per-point failure (quality='bad') must not sink the whole batch."""
    proto = SiemensS7Protocol({"source_ip": "x"})
    proto.is_connected = True

    def fake_read_points(points):
        return [
            {"code": "OK", "value": 5, "timestamp": 1, "quality": "good"},
            {"code": "BAD", "value": None, "timestamp": 2, "quality": "bad"},
        ]

    monkeypatch.setattr(proto, "read_points", fake_read_points)

    readings = proto.read_batch(_group("OK", "BAD"))

    by_code = {r.point_code: r for r in readings}
    assert by_code["OK"].quality == "good" and by_code["OK"].value == 5
    assert by_code["BAD"].quality == "bad" and by_code["BAD"].value is None


def test_default_read_batch_reraises_transport_readerror(monkeypatch):
    """A whole-batch transport failure (read_points raising ReadError) propagates
    as ReadError so the pipeline reconnects — matching the Modbus contract."""
    from acquisition.protocols.base import ReadError

    proto = OPCUAProtocol({"endpoint_url": "opc.tcp://x"})
    proto.is_connected = True

    def boom(points):
        raise ReadError("session dropped")

    monkeypatch.setattr(proto, "read_points", boom)

    with pytest.raises(ReadError):
        proto.read_batch(_group("A"))


def test_default_read_batch_wraps_unexpected_exception_as_readerror(monkeypatch):
    """A non-ReadError raised by read_points is wrapped as ReadError, never
    silently swallowed, so the pipeline still treats it as a failed cycle."""
    from acquisition.protocols.base import ReadError

    proto = MitsubishiPLCProtocol({"source_ip": "x"})
    proto.is_connected = True

    def boom(points):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(proto, "read_points", boom)

    with pytest.raises(ReadError):
        proto.read_batch(_group("A"))
