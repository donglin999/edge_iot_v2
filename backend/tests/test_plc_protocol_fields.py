"""Regression tests for rank5: Mitsubishi MC protocol (plc.py) FieldSpec/META.

Before this fix ``MitsubishiPLCProtocol`` inherited ``BaseProtocol``'s empty
defaults for ``META``/``DEVICE_FIELDS``/``POINT_FIELDS``/``IDENTITY_FIELDS``:

* the protocol picker rendered it as "Base" instead of a real label,
* the dynamic device/point forms had nothing to render,
* the Excel template generator produced no columns for it,
* import-time ``validate_device``/``validate_point`` always passed (nothing
  to check against),
* an empty ``IDENTITY_FIELDS`` meant every MC device shared the same empty
  identity tuple ``()`` and got merged into a single device on import,
* ``source_port`` was only ever read in ``__init__`` from a raw dict key,
  never validated/exposed as a configurable field.

These tests pin the fixed schema and its effects. They exercise the class
directly (not through ``ProtocolRegistry``) since ``plc.py`` is deliberately
not imported by ``protocols/__init__.py`` yet (see the NOTE there) — that is
a separate, intentional decision documented by
``test_e2e_all_protocols.py::test_every_user_facing_protocol_has_a_script``.
"""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from acquisition.protocols.base import FieldSpec, ProtocolMeta
from acquisition.protocols.plc import MitsubishiPLCProtocol, _MC_DATA_TYPES


# ---------------------------------------------------------------------------
# META
# ---------------------------------------------------------------------------


def test_meta_is_overridden_not_base():
    meta = MitsubishiPLCProtocol.META
    assert isinstance(meta, ProtocolMeta)
    assert meta.name in ("mc", "plc")
    assert meta.label != "Base"
    assert meta.category == "industrial-ethernet"


# ---------------------------------------------------------------------------
# DEVICE_FIELDS
# ---------------------------------------------------------------------------


def test_device_fields_declared():
    names = {f.name for f in MitsubishiPLCProtocol.DEVICE_FIELDS}
    assert {"source_ip", "source_port"} <= names

    by_name = {f.name: f for f in MitsubishiPLCProtocol.DEVICE_FIELDS}
    assert by_name["source_ip"].required is True
    assert by_name["source_port"].kind == "int"
    assert by_name["source_port"].default == 6000
    # help_text must call out that it needs to match the PLC-side setting.
    assert "PLC" in by_name["source_port"].help_text


def test_device_fields_missing_source_ip_fails_validation():
    errors = MitsubishiPLCProtocol.validate_device({"source_port": 6000})
    assert errors, "source_ip is required and must be flagged when missing"


def test_device_fields_valid_config_passes_validation():
    errors = MitsubishiPLCProtocol.validate_device(
        {"source_ip": "10.0.0.5", "source_port": 6000}
    )
    assert errors == []


# ---------------------------------------------------------------------------
# IDENTITY_FIELDS — the de-dup bug
# ---------------------------------------------------------------------------


def test_identity_fields_not_empty():
    assert MitsubishiPLCProtocol.IDENTITY_FIELDS == ("source_ip", "source_port")


def test_identity_fields_distinguish_two_devices():
    """Two MC devices with different IPs must not collapse into one."""
    row_a = {"source_ip": "10.0.0.1", "source_port": 6000}
    row_b = {"source_ip": "10.0.0.2", "source_port": 6000}

    identity_a = tuple(row_a.get(f) for f in MitsubishiPLCProtocol.IDENTITY_FIELDS)
    identity_b = tuple(row_b.get(f) for f in MitsubishiPLCProtocol.IDENTITY_FIELDS)

    assert identity_a != identity_b
    # Before the fix, IDENTITY_FIELDS == () so both identities were the
    # empty tuple and every MC device merged into one.
    assert identity_a != ()
    assert identity_b != ()


# ---------------------------------------------------------------------------
# POINT_FIELDS — must align with _group_by_type's dispatch keys
# ---------------------------------------------------------------------------


def test_point_fields_declared():
    names = {f.name for f in MitsubishiPLCProtocol.POINT_FIELDS}
    assert {"code", "address", "type", "num", "coefficient", "precision"} <= names


def test_point_type_enum_matches_group_by_type_dispatch():
    """The 'type' FieldSpec choices must be exactly the keys read_points()'s
    _group_by_type dispatch actually handles — note the dispatch key is
    'str', NOT 'string' (a mismatch here silently falls through to the
    'unsupported data type' bad-quality branch)."""
    by_name = {f.name: f for f in MitsubishiPLCProtocol.POINT_FIELDS}
    type_spec = by_name["type"]
    assert type_spec.kind == "enum"
    assert set(type_spec.choices) == {"int16", "int32", "float", "bool", "str"}
    assert "string" not in type_spec.choices
    assert set(type_spec.choices) == set(_MC_DATA_TYPES)


def test_point_type_choices_are_all_actually_dispatched():
    """Every declared choice must be handled by read_points()'s type-group
    dispatch (not fall through to the 'unsupported data type' branch)."""
    proto = MitsubishiPLCProtocol({"source_ip": "10.0.0.1"})

    class _FakePLC:
        def ReadInt16(self, addr, num):
            return SimpleNamespace(IsSuccess=True, Content=[0])

        def ReadInt32(self, addr, num):
            return SimpleNamespace(IsSuccess=True, Content=[0])

        def ReadFloat(self, addr, num):
            return SimpleNamespace(IsSuccess=True, Content=[0.0])

        def ReadBool(self, addr, num):
            return SimpleNamespace(IsSuccess=True, Content=[False])

        def ReadString(self, addr, num):
            return SimpleNamespace(IsSuccess=True, Content="")

        def Read(self, addr, length):
            return SimpleNamespace(IsSuccess=True, Content=[0] * (2 * length))

    proto.plc = _FakePLC()
    proto.is_connected = True

    by_name = {f.name: f for f in MitsubishiPLCProtocol.POINT_FIELDS}
    for data_type in by_name["type"].choices:
        points = [{"code": "p", "address": "D100", "type": data_type, "num": 1}]
        results = proto.read_points(points)
        assert len(results) == 1
        # Only the "unsupported data type" fallback branch sets this exact
        # error message (and only it uses this f-string) — success paths
        # for int16/int32/float/bool/str never set an "error" key at all.
        assert results[0].get("error") != f"Unsupported data type: {data_type}", (
            f"declared choice {data_type!r} is not dispatched by _group_by_type"
        )


# ---------------------------------------------------------------------------
# network_no / station_no — configurable, not hardcoded
# ---------------------------------------------------------------------------


def test_network_and_station_number_default_to_zero():
    proto = MitsubishiPLCProtocol({"source_ip": "10.0.0.1"})
    assert proto.network_no == 0
    assert proto.station_no == 0


def test_network_and_station_number_read_from_config():
    proto = MitsubishiPLCProtocol(
        {"source_ip": "10.0.0.1", "network_no": 3, "station_no": 5}
    )
    assert proto.network_no == 3
    assert proto.station_no == 5


def test_network_station_fields_declared_and_optional():
    by_name = {f.name: f for f in MitsubishiPLCProtocol.DEVICE_FIELDS}
    assert "network_no" in by_name and "station_no" in by_name
    assert by_name["network_no"].required is False
    assert by_name["station_no"].required is False


def _install_fake_hsl(monkeypatch, mcnet_cls) -> None:
    """Inject a fake ``lib.HslCommunication`` module so ``connect()``'s
    local ``from lib.HslCommunication import MelsecMcNet`` resolves without
    the real (not vendored in this repo) library."""
    fake_hsl = types.ModuleType("lib.HslCommunication")
    fake_hsl.MelsecMcNet = mcnet_cls
    lib_pkg = types.ModuleType("lib")
    lib_pkg.HslCommunication = fake_hsl
    monkeypatch.setitem(sys.modules, "lib", lib_pkg)
    monkeypatch.setitem(sys.modules, "lib.HslCommunication", fake_hsl)


def test_connect_sets_network_station_when_lib_supports_it(monkeypatch):
    """connect() must apply network_no/station_no onto the underlying MC
    client best-effort, without assuming a specific HslCommunication API."""

    class _FakeMcNet:
        def __init__(self, ip, port):
            self.ip = ip
            self.port = port
            self.NetworkNumber = None
            self.NetworkStationNumber = None

        def ConnectServer(self):
            return SimpleNamespace(IsSuccess=True, Message="")

    _install_fake_hsl(monkeypatch, _FakeMcNet)

    proto = MitsubishiPLCProtocol(
        {"source_ip": "10.0.0.1", "network_no": 7, "station_no": 2}
    )
    assert proto.connect() is True
    assert proto.plc.NetworkNumber == 7
    assert proto.plc.NetworkStationNumber == 2


def test_connect_does_not_crash_when_lib_lacks_network_attrs(monkeypatch):
    """If the installed HslCommunication build has no NetworkNumber /
    NetworkStationNumber attributes, connect() must not blow up."""

    class _FakeMcNetNoAttrs:
        def __init__(self, ip, port):
            pass

        def ConnectServer(self):
            return SimpleNamespace(IsSuccess=True, Message="")

    _install_fake_hsl(monkeypatch, _FakeMcNetNoAttrs)

    proto = MitsubishiPLCProtocol(
        {"source_ip": "10.0.0.1", "network_no": 7, "station_no": 2}
    )
    assert proto.connect() is True  # must not raise AttributeError
