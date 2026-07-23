"""Regression tests for rank20: assorted protocol FieldSpec clean-ups.

(a) Modbus RTU ``bytesize`` used to offer ``7`` as a choice. An RTU frame is
    always 8 data bits — 7 data bits is Modbus *ASCII*, a different framing
    RTU masters don't speak — so a config with ``bytesize=7`` builds a
    non-conformant frame silently.

(b) ``required=True`` paired with a non-``None`` ``default`` is a no-op in
    ``base.py``'s validator (``if spec.required and spec.default is None``),
    so the frontend drew a misleading required-field asterisk on
    ``scada_topic_template`` (scada.py) and ``source_ip`` (simulator.py)
    that import-time validation never actually enforced. Both are now
    ``required=False`` — the default already covers the missing case.
"""
from __future__ import annotations

from acquisition.protocols.modbus import ModbusRTUProtocol
from acquisition.protocols.scada import SCADAProtocol, DEFAULT_TOPIC_TEMPLATE
from acquisition.protocols.simulator import SimulatorProtocol


# ---------------------------------------------------------------------------
# (a) Modbus RTU bytesize
# ---------------------------------------------------------------------------


def test_rtu_bytesize_only_offers_8():
    by_name = {f.name: f for f in ModbusRTUProtocol.DEVICE_FIELDS}
    spec = by_name["bytesize"]
    assert spec.choices == (8,)
    assert spec.default == 8
    assert "8" in spec.help_text


def test_rtu_validate_device_rejects_bytesize_7():
    errors = ModbusRTUProtocol.validate_device({
        "serial_port": "/dev/ttyUSB0", "slave_id": 1, "bytesize": 7,
    })
    assert errors, "bytesize=7 (ASCII framing) must be rejected for RTU"


def test_rtu_validate_device_accepts_bytesize_8():
    errors = ModbusRTUProtocol.validate_device({
        "serial_port": "/dev/ttyUSB0", "slave_id": 1, "bytesize": 8,
    })
    assert errors == []


# ---------------------------------------------------------------------------
# (b) required=True + non-None default was a no-op — now required=False
# ---------------------------------------------------------------------------


def test_scada_topic_template_no_longer_marked_required():
    by_name = {f.name: f for f in SCADAProtocol.DEVICE_FIELDS}
    spec = by_name["scada_topic_template"]
    assert spec.required is False
    assert spec.default == DEFAULT_TOPIC_TEMPLATE


def test_scada_validate_device_passes_when_topic_template_omitted():
    errors = SCADAProtocol.validate_device({
        "source_ip": "10.134.14.147",
        "scada_product_key": "pk",
        "scada_device_name": "dn",
        # scada_topic_template intentionally omitted
    })
    assert errors == []


def test_scada_coerce_device_fills_default_topic_template():
    coerced = SCADAProtocol.coerce_device({
        "source_ip": "10.134.14.147",
        "scada_product_key": "pk",
        "scada_device_name": "dn",
    })
    assert coerced["scada_topic_template"] == DEFAULT_TOPIC_TEMPLATE


def test_simulator_source_ip_no_longer_marked_required():
    by_name = {f.name: f for f in SimulatorProtocol.DEVICE_FIELDS}
    spec = by_name["source_ip"]
    assert spec.required is False
    assert spec.default == "sim-1"


def test_simulator_validate_device_passes_when_source_ip_omitted():
    errors = SimulatorProtocol.validate_device({})
    assert errors == []


def test_simulator_coerce_device_fills_default_source_ip():
    coerced = SimulatorProtocol.coerce_device({})
    assert coerced["source_ip"] == "sim-1"
