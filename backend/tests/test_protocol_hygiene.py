"""Regression tests for protocol-layer engineering-hygiene fixes (XIU-6).

Covers six independent fixes:

* **M7** – Modbus TCP enables ``SO_KEEPALIVE`` on connect.
* **M8** – MQTT ``read_points()`` timeout is configurable (``mqtt_read_timeout``).
* **M9** – ``ReadWorker`` auto-timeout is floored at 500 ms.
* **L1** – legacy Modbus ``read_points()`` goes through the gap-tolerant
  ``ReadPlanBuilder`` batch merge instead of strict-contiguous grouping.
* **L2** – ``read_plan`` never appends a degenerate (empty / 0-register) group.
* **L3** – S7 address parsing rejects a bool bit offset > 7.

The suite avoids real network I/O — Modbus masters are faked, and the S7
parser is exercised with a stand-in ``Area`` enum so it runs without
python-snap7 installed.
"""
from __future__ import annotations

import socket
import threading
from types import SimpleNamespace

import pytest


# ===========================================================================
# M7 — Modbus TCP SO_KEEPALIVE
# ===========================================================================


class _FakeTcpMaster:
    """Stand-in for ``modbus_tk.modbus_tcp.TcpMaster`` with a real socket."""

    def __init__(self, host, port, timeout_in_sec):
        self.host = host
        self.port = port
        self.timeout_in_sec = timeout_in_sec
        self.opened = False
        # A real (unconnected) socket so setsockopt / getsockopt behave.
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    def open(self):
        self.opened = True

    def close(self):
        self._sock.close()


def test_m7_modbus_tcp_enables_keepalive(monkeypatch):
    from acquisition.protocols import modbus

    monkeypatch.setattr(modbus.modbus_tcp, "TcpMaster", _FakeTcpMaster)

    proto = modbus.ModbusTCPProtocol(
        {"source_ip": "192.0.2.10", "source_port": 502, "slave_id": 1}
    )
    assert proto.connect() is True
    assert proto.master.opened is True, "socket must be opened so it can be tuned"

    # getsockopt returns a non-zero value when the option is set; on macOS
    # it echoes the flag bit (8) rather than 1, so assert truthiness.
    keepalive = proto.master._sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
    assert keepalive != 0, "SO_KEEPALIVE must be enabled on the Modbus TCP socket"

    proto.master.close()


def test_m7_keepalive_missing_socket_is_safe(monkeypatch):
    """``_enable_keepalive`` must not blow up if the master has no ``_sock``."""
    from acquisition.protocols import modbus

    proto = modbus.ModbusTCPProtocol({"source_ip": "192.0.2.10"})
    proto.master = SimpleNamespace()  # no _sock attribute
    proto._enable_keepalive()  # should be a silent no-op


# ===========================================================================
# M8 — MQTT configurable read timeout
# ===========================================================================


def test_m8_mqtt_read_timeout_configurable():
    from acquisition.protocols.mqtt import MQTTProtocol

    proto = MQTTProtocol(
        {"source_ip": "broker", "source_port": 1883, "mqtt_read_timeout": 2.5}
    )
    assert proto.read_timeout == 2.5


def test_m8_mqtt_read_timeout_defaults_and_guards():
    from acquisition.protocols.mqtt import MQTTProtocol

    # Default when unset.
    assert MQTTProtocol({"source_ip": "b"}).read_timeout == 5.0
    # Garbage falls back to the default.
    assert MQTTProtocol({"source_ip": "b", "mqtt_read_timeout": "abc"}).read_timeout == 5.0
    # Non-positive values fall back to the default.
    assert MQTTProtocol({"source_ip": "b", "mqtt_read_timeout": 0}).read_timeout == 5.0
    assert MQTTProtocol({"source_ip": "b", "mqtt_read_timeout": -3}).read_timeout == 5.0


# ===========================================================================
# M9 — ReadWorker auto-timeout floor (500 ms)
# ===========================================================================


def _make_worker(sample_rate_hz, configured_timeout=5.0):
    from acquisition.services.pipeline import ReadWorker

    device = SimpleNamespace(
        code="DEV1",
        id=1,
        protocol="modbus_tcp",
        metadata={"timeout": configured_timeout},
    )
    return ReadWorker(
        device,
        [],  # no points -> empty read plan
        [],  # no sinks
        sample_rate_hz=sample_rate_hz,
        shutdown_event=threading.Event(),
        health_dict={},
    )


def test_m9_auto_timeout_floored_at_500ms():
    # 20 Hz -> cycle 50 ms -> cycle*2 = 100 ms, which is below the floor.
    worker = _make_worker(sample_rate_hz=20.0)
    assert worker._auto_timeout == 0.5


def test_m9_auto_timeout_not_floored_when_cycle_is_slow():
    # 1 Hz -> cycle 1 s -> cycle*2 = 2 s, well above the floor and below the
    # configured 5 s ceiling.
    worker = _make_worker(sample_rate_hz=1.0)
    assert worker._auto_timeout == pytest.approx(2.0)


# ===========================================================================
# L1 — legacy Modbus read_points() uses the batch-merge plan
# ===========================================================================


class _RecordingMaster:
    """Fake Modbus master that records every ``execute`` call."""

    def __init__(self):
        self.calls = []

    def execute(self, slave, function_code, starting_address, quantity_of_x):
        self.calls.append((function_code, starting_address, quantity_of_x))
        # Return register words 0,1,2,... so decoded values are predictable.
        return tuple(range(quantity_of_x))


def test_l1_read_points_merges_across_small_gap():
    """Two FC3 points 3 registers apart used to be 2 reads (strict-contiguous);
    with the gap-tolerant plan they collapse into a single ``execute`` call."""
    from acquisition.protocols import modbus

    proto = modbus.ModbusTCPProtocol({"source_ip": "192.0.2.10"})
    master = _RecordingMaster()
    proto.master = master
    proto.is_connected = True

    points = [
        {"code": "a", "address": "0", "function_code": 3, "data_type": "uint16"},
        {"code": "b", "address": "3", "function_code": 3, "data_type": "uint16"},
    ]
    results = proto.read_points(points)

    assert len(master.calls) == 1, f"expected one merged read, got {master.calls}"
    fc, start, count = master.calls[0]
    assert (fc, start) == (3, 0)
    assert count == 4  # spans address 0..3 inclusive

    by_code = {r["code"]: r for r in results}
    assert set(by_code) == {"a", "b"}
    # data words are range(4) -> point a@offset0=0, point b@offset3=3
    assert by_code["a"]["value"] == 0
    assert by_code["b"]["value"] == 3
    assert all(r["quality"] == "good" for r in results)


def test_l1_distinct_function_codes_stay_separate():
    """FC3 and FC4 target different memory areas and must not be merged."""
    from acquisition.protocols import modbus

    proto = modbus.ModbusTCPProtocol({"source_ip": "192.0.2.10"})
    master = _RecordingMaster()
    proto.master = master
    proto.is_connected = True

    points = [
        {"code": "h", "address": "0", "function_code": 3, "data_type": "uint16"},
        {"code": "i", "address": "0", "function_code": 4, "data_type": "uint16"},
    ]
    proto.read_points(points)
    assert len(master.calls) == 2
    assert {c[0] for c in master.calls} == {3, 4}


# ===========================================================================
# L2 — read_plan never appends a degenerate group
# ===========================================================================


def _stub_point(code, address, fc=3, data_type="uint16", num=None):
    extra = {"function_code": fc, "data_type": data_type}
    if num is not None:
        extra["num"] = num
    return SimpleNamespace(code=code, address=str(address), extra=extra, template=None)


@pytest.mark.parametrize(
    "points",
    [
        [],  # empty input
        [_stub_point("p0", 0)],  # single point
        [_stub_point("p0", 0), _stub_point("p1", 0)],  # exact duplicate
        [_stub_point(f"p{i}", i * 50) for i in range(5)],  # far-apart -> many groups
        [_stub_point("p0", 0, fc=3), _stub_point("p1", 0, fc=4)],  # split by fc
    ],
)
def test_l2_no_degenerate_groups(points):
    from acquisition.services.read_plan import ReadPlanBuilder

    device = SimpleNamespace(protocol="modbus_tcp", metadata={"slave_id": 1})
    groups = ReadPlanBuilder.build(device, points)
    for g in groups:
        assert g.points, "a read group must never be empty"
        assert g.register_count is not None and g.register_count > 0, (
            f"degenerate group with register_count={g.register_count}"
        )
        assert g.start_address is not None


# ===========================================================================
# L3 — S7 rejects bool bit offset > 7
# ===========================================================================


@pytest.fixture
def s7_with_area(monkeypatch):
    """Make ``_parse_s7_address`` usable without python-snap7 installed by
    supplying a stand-in ``Area`` enum and flipping the availability flag."""
    from acquisition.protocols import s7

    fake_area = SimpleNamespace(DB="DB", MK="MK", PE="PE", PA="PA")
    monkeypatch.setattr(s7, "_SNAP7_AVAILABLE", True)
    monkeypatch.setattr(s7, "Area", fake_area)
    return s7


@pytest.mark.parametrize("address", ["DB1.DBX10.8", "M5.9", "I0.15", "DB2.DBX0.99"])
def test_l3_bit_offset_above_7_rejected(s7_with_area, address):
    with pytest.raises(ValueError, match="位偏移"):
        s7_with_area._parse_s7_address(address)


@pytest.mark.parametrize("address", ["DB1.DBX10.0", "DB1.DBX10.7", "M5.3", "I0.0"])
def test_l3_bit_offset_within_range_accepted(s7_with_area, address):
    area, db, byte, bit, length = s7_with_area._parse_s7_address(address)
    assert 0 <= bit <= 7
    assert length == 1
