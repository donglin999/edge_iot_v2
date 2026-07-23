"""Regression tests for rank19: S7 LReal / float64 (8-byte double) support.

Before the fix ``_S7_DATA_TYPES`` had no float64/lreal entry and ``_decode``
had no 8-byte double branch, so S7-1200/1500 LReal tags (very common —
they're the default "real number" type on modern TIA projects) could not be
configured at all.

Fixing this needs two coordinated changes, not just a ``_decode`` branch:
the byte length read off the wire for a "D" (double-word) address is
normally 4 bytes (``DBD``/``MD``/``ID``/``QD``); an 8-byte LReal needs the
*read* widened to 8 bytes too, or ``_decode`` gets handed a too-short buffer.
``_parse_s7_address`` now takes an optional ``data_type`` hint for exactly
this.

Reuses the ``s7_with_area`` fixture pattern from
``test_protocol_hygiene.py`` (fake ``Area`` enum, no python-snap7 needed).
"""
from __future__ import annotations

import struct
from types import SimpleNamespace

import pytest


@pytest.fixture
def s7_with_area(monkeypatch):
    from acquisition.protocols import s7

    fake_area = SimpleNamespace(DB="DB", MK="MK", PE="PE", PA="PA")
    monkeypatch.setattr(s7, "_SNAP7_AVAILABLE", True)
    monkeypatch.setattr(s7, "Area", fake_area)
    return s7


# ---------------------------------------------------------------------------
# _S7_DATA_TYPES / _decode
# ---------------------------------------------------------------------------


def test_float64_and_lreal_are_declared_types():
    from acquisition.protocols.s7 import _S7_DATA_TYPES

    assert "float64" in _S7_DATA_TYPES
    assert "lreal" in _S7_DATA_TYPES


@pytest.mark.parametrize("data_type", ["float64", "lreal", "FLOAT64", "LReal"])
def test_decode_double_branch(data_type):
    from acquisition.protocols.s7 import _decode

    buf = struct.pack(">d", 3.14159265358979)
    value = _decode(buf, data_type, bit=0)
    assert value == pytest.approx(3.14159265358979)


def test_decode_float32_still_4_bytes_unaffected():
    from acquisition.protocols.s7 import _decode

    buf = struct.pack(">f", 2.5)
    assert _decode(buf, "float32", bit=0) == pytest.approx(2.5)


# ---------------------------------------------------------------------------
# _parse_s7_address — read length widening
# ---------------------------------------------------------------------------


def test_dbd_address_default_length_is_still_4_bytes(s7_with_area):
    """Back-compat: no data_type hint (or a non-double one) keeps the
    existing 4-byte DBD/int32/float32 behaviour untouched."""
    area, db, byte, bit, length = s7_with_area._parse_s7_address("DB1.DBD0")
    assert length == 4

    area, db, byte, bit, length = s7_with_area._parse_s7_address("DB1.DBD0", "float32")
    assert length == 4

    area, db, byte, bit, length = s7_with_area._parse_s7_address("DB1.DBD0", "int32")
    assert length == 4


@pytest.mark.parametrize("data_type", ["float64", "lreal"])
def test_dbd_address_widens_to_8_bytes_for_double(s7_with_area, data_type):
    area, db, byte, bit, length = s7_with_area._parse_s7_address("DB1.DBD8", data_type)
    assert length == 8
    assert byte == 8  # start byte unchanged, only the length widens


@pytest.mark.parametrize("prefix,data_type", [
    ("MD", "float64"), ("ID", "lreal"), ("QD", "float64"),
])
def test_other_dword_areas_also_widen(s7_with_area, prefix, data_type):
    area, db, byte, bit, length = s7_with_area._parse_s7_address(f"{prefix}4", data_type)
    assert length == 8


# ---------------------------------------------------------------------------
# End-to-end via read_points()
# ---------------------------------------------------------------------------


def test_read_points_decodes_lreal_end_to_end(s7_with_area, monkeypatch):
    proto = s7_with_area.SiemensS7Protocol({"source_ip": "10.0.0.1"})

    expected = 12345.6789
    payload = struct.pack(">d", expected)

    class _FakeClient:
        def get_connected(self):
            return True

        def read_area(self, area, db, byte, length):
            assert length == 8, "must request 8 bytes for an LReal read"
            return bytearray(payload)

    proto.client = _FakeClient()
    proto.is_connected = True

    results = proto.read_points([
        {"code": "temp_precise", "address": "DB1.DBD0", "data_type": "lreal"},
    ])

    assert len(results) == 1
    assert results[0]["quality"] == "good"
    assert results[0]["value"] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# help_text call-outs (S7-200 TSAP caveat, string length caveat)
# ---------------------------------------------------------------------------


def test_plc_type_help_text_calls_out_s7_200():
    from acquisition.protocols.s7 import SiemensS7Protocol

    by_name = {f.name: f for f in SiemensS7Protocol.DEVICE_FIELDS}
    assert "S7-200" in by_name["plc_type"].help_text
    assert "TSAP" in by_name["plc_type"].help_text


def test_data_type_help_text_documents_string_length_limit():
    from acquisition.protocols.s7 import SiemensS7Protocol

    by_name = {f.name: f for f in SiemensS7Protocol.POINT_FIELDS}
    help_text = by_name["data_type"].help_text
    assert "string" in help_text or "字符串" in help_text
    assert "lreal" in help_text.lower() or "float64" in help_text.lower()
