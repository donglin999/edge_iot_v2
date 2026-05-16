"""Targeted regression tests for the XIU-2 critical data-correctness fixes.

Covers C1 (Modbus byte order), C7 (PLC batch-read length), C3 (AlarmSink flush
deadlock), C4 (InfluxDBSink concurrent flush), C5 (Excel streaming load +
chunked transactions) and C6 (SQLite tuning + WS status query collapse).
"""
from __future__ import annotations

import struct
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.fixtures.factories import *  # noqa: F401,F403 — pytest fixtures


# ===========================================================================
# C1 — Modbus byte order
# ===========================================================================

from acquisition.protocols.modbus import (
    _apply_byte_order,
    _combine_registers,
    _struct_prefix,
)


def _registers_for(be_bytes: bytes, order: str):
    """Lay ``be_bytes`` (canonical big-endian IEEE/integer bytes) onto uint16
    registers exactly the way a device using ``order`` would.

    This mirrors the *spec* of each byte order (the docstring contract), not
    the implementation under test — so a passing assertion proves the decoder
    agrees with the spec.
    """
    words = [be_bytes[i : i + 2] for i in range(0, len(be_bytes), 2)]
    if order == "big":            # ABCD
        seq = words
    elif order == "little":       # DCBA — every byte reversed
        seq = [w[::-1] for w in reversed(words)]
    elif order == "big-swap":     # BADC — bytes swapped within each word
        seq = [w[::-1] for w in words]
    elif order == "little-swap":  # CDAB — word order reversed
        seq = list(reversed(words))
    else:
        raise ValueError(order)
    return [int.from_bytes(w, "big") for w in seq]


_ALL_ORDERS = ("big", "little", "big-swap", "little-swap")


class TestC1ModbusByteOrder:
    @pytest.mark.parametrize("order", _ALL_ORDERS)
    @pytest.mark.parametrize("value", [1.0, 25.5, -273.15, 3.4028235e38])
    def test_float32_all_orders(self, order, value):
        regs = _registers_for(struct.pack(">f", value), order)
        decoded = _combine_registers(regs, "float32", 2, order)
        assert decoded == pytest.approx(value, rel=1e-6)

    @pytest.mark.parametrize("order", _ALL_ORDERS)
    @pytest.mark.parametrize("value", [0.0, -1.5, 123456.789, 2.2250738585072014e-308])
    def test_float64_all_orders(self, order, value):
        regs = _registers_for(struct.pack(">d", value), order)
        decoded = _combine_registers(regs, "float64", 4, order)
        assert decoded == pytest.approx(value, rel=1e-12)

    @pytest.mark.parametrize("order", _ALL_ORDERS)
    @pytest.mark.parametrize("value", [0, 1, -1, 2147483647, -2147483648, 70000])
    def test_int32_all_orders(self, order, value):
        regs = _registers_for(struct.pack(">i", value), order)
        assert _combine_registers(regs, "int32", 2, order) == value

    @pytest.mark.parametrize("order", _ALL_ORDERS)
    @pytest.mark.parametrize("value", [0, 1, 4294967295, 3000000000])
    def test_uint32_all_orders(self, order, value):
        regs = _registers_for(struct.pack(">I", value), order)
        assert _combine_registers(regs, "uint32", 2, order) == value

    @pytest.mark.parametrize("order", _ALL_ORDERS)
    def test_int64_all_orders(self, order):
        value = -1234567890123
        regs = _registers_for(struct.pack(">q", value), order)
        assert _combine_registers(regs, "int64", 4, order) == value

    def test_little_swap_returns_bytes_not_iterator(self):
        # The original bug report flagged the little-swap branch returning an
        # iterator; assert the helper always yields a concrete bytes object.
        out = _apply_byte_order(b"\x01\x02\x03\x04", "little-swap")
        assert isinstance(out, bytes)
        assert len(out) == 4

    def test_struct_prefix_selection(self):
        assert _struct_prefix("big") == ">"
        assert _struct_prefix("big-swap") == ">"
        assert _struct_prefix("little") == "<"
        assert _struct_prefix("little-swap") == "<"
        assert _struct_prefix("") == ">"          # default
        assert _struct_prefix("nonsense") == ">"  # unknown -> default

    def test_single_register_passthrough(self):
        assert _combine_registers([42], "uint16", 1, "big") == 42
        assert _combine_registers([1], "bool", 1, "big") is True
        assert _combine_registers([0], "bool", 1, "big") is False


# ===========================================================================
# C7 — Mitsubishi PLC batch-read length (no off-by-one)
# ===========================================================================

from acquisition.protocols.plc import MitsubishiPLCProtocol


class _FakePLC:
    """Records every Read(addr, length) and returns zero-filled words."""

    def __init__(self):
        self.calls = []

    def Read(self, addr, length):
        self.calls.append((addr, length))
        return SimpleNamespace(IsSuccess=True, Content=[0] * (2 * length))


def _plc():
    proto = MitsubishiPLCProtocol({"source_ip": "10.0.0.1", "source_port": 6000})
    proto.plc = _FakePLC()
    proto.is_connected = True
    return proto


class TestC7PLCBatchLength:
    def test_single_register_reads_exactly_one_word(self):
        proto = _plc()
        proto._read_int16([{"code": "c1", "address": "D100", "type": "int16"}])
        # Off-by-one would request length 0 here.
        assert proto.plc.calls == [("D100", 1)]

    def test_continuous_group_spans_all_words(self):
        proto = _plc()
        points = [
            {"code": "a", "address": "D100", "type": "int16"},
            {"code": "b", "address": "D101", "type": "int16"},
            {"code": "c", "address": "D102", "type": "int16"},
        ]
        proto._read_int16(points)
        assert proto.plc.calls == [("D100", 3)]

    def test_multi_word_last_register_is_fully_covered(self):
        proto = _plc()
        # D100 occupies words 100-101, D102 word 102 -> 3 words total.
        points = [
            {"code": "a", "address": "D100", "type": "int16", "num": 2},
            {"code": "b", "address": "D102", "type": "int16", "num": 1},
        ]
        proto._read_int16(points)
        assert proto.plc.calls == [("D100", 3)]

    def test_discontinuous_points_split_into_separate_reads(self):
        proto = _plc()
        points = [
            {"code": "a", "address": "D100", "type": "int16"},
            {"code": "b", "address": "D105", "type": "int16"},
        ]
        proto._read_int16(points)
        assert sorted(proto.plc.calls) == [("D100", 1), ("D105", 1)]


# ===========================================================================
# C3 — AlarmSink flush must not deadlock when evaluation raises
# ===========================================================================

from acquisition.services.read_plan import Reading
from acquisition.services.sinks import AlarmSink, InfluxDBSink


def _reading(code="p1", value=1.0, quality="good"):
    return Reading(point_code=code, value=value, timestamp_ns=1, quality=quality)


def _alarm_device_groups():
    device = SimpleNamespace(code="dev1")
    return {1: {"device": device, "points": [{"code": "p1", "coefficient": 1.0, "precision": 2}]}}


class TestC3AlarmSinkFlush:
    def test_flush_returns_quickly_when_evaluation_raises(self, monkeypatch):
        # Force every evaluation to blow up — task_done() must still run in the
        # writer's finally block, otherwise flush() blocks for its full 5 s.
        def _boom(self, reading, meta):
            raise RuntimeError("evaluate exploded")

        monkeypatch.setattr(AlarmSink, "_evaluate_one", _boom)
        sink = AlarmSink(SimpleNamespace(id=99), _alarm_device_groups())
        try:
            for _ in range(10):
                sink.consume(_reading())
            started = time.time()
            sink.flush()
            elapsed = time.time() - started
            # A skipped task_done() would make this ~5 s (the flush timeout).
            assert elapsed < 4.0
            assert sink._queue.unfinished_tasks == 0
        finally:
            sink.close()

    def test_flush_drains_successful_readings(self, monkeypatch):
        seen = []
        monkeypatch.setattr(AlarmSink, "_evaluate_one",
                            lambda self, r, m: seen.append(r.point_code))
        sink = AlarmSink(SimpleNamespace(id=98), _alarm_device_groups())
        try:
            for _ in range(5):
                sink.consume(_reading())
            sink.flush()
            assert len(seen) == 5
            assert sink._queue.unfinished_tasks == 0
        finally:
            sink.close()


# ===========================================================================
# C4 — InfluxDBSink concurrent flush is atomic
# ===========================================================================

def _influx_point(i=0):
    return {"measurement": "m", "tags": {}, "fields": {"p": i}, "time": i}


class _FailStorage:
    def __init__(self):
        self.write_count = 0

    def write(self, batch):
        self.write_count += 1
        raise RuntimeError("influx down")


class _BlockingStorage:
    def __init__(self):
        self.write_count = 0
        self.started = threading.Event()
        self.release = threading.Event()

    def write(self, batch):
        self.write_count += 1
        self.started.set()
        self.release.wait(timeout=5)


class TestC4InfluxDBSinkFlush:
    def test_backoff_gate_blocks_immediate_retry(self, monkeypatch):
        storage = _FailStorage()
        monkeypatch.setattr(InfluxDBSink, "_init_storage", lambda self: storage)
        sink = InfluxDBSink(SimpleNamespace(id=1), {})
        sink._buffer = [_influx_point()]

        sink.flush()
        assert storage.write_count == 1
        assert sink._fail_count == 1
        assert sink._next_flush_at > time.time()
        assert sink._flush_in_progress is False
        assert sink._buffer, "failed batch must stay buffered for retry"

        # The backoff gate (now read under the lock) must reject an immediate
        # second flush instead of pounding the sick backend again.
        sink.flush()
        assert storage.write_count == 1

    def test_concurrent_flush_does_not_double_write(self, monkeypatch):
        storage = _BlockingStorage()
        monkeypatch.setattr(InfluxDBSink, "_init_storage", lambda self: storage)
        sink = InfluxDBSink(SimpleNamespace(id=2), {})
        sink._buffer = [_influx_point(i) for i in range(3)]

        worker = threading.Thread(target=sink.flush)
        worker.start()
        try:
            assert storage.started.wait(timeout=2), "first flush never started"
            # A second caller while the first holds _flush_in_progress must be
            # a no-op — without the guard it would snapshot and re-write.
            sink.flush()
            assert storage.write_count == 1
        finally:
            storage.release.set()
            worker.join(timeout=5)

        assert storage.write_count == 1
        assert sink._buffer == []          # written prefix removed exactly once
        assert sink._total_written == 3
        assert sink._flush_in_progress is False


# ===========================================================================
# C5 — Excel streaming load + chunked transactions
# ===========================================================================

from configuration.services.importer import ExcelImportService


def _write_xlsx(path, header, rows):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(list(header))
    for row in rows:
        ws.append(list(row))
    wb.save(path)


class TestC5ImporterStreaming:
    def test_load_dataframe_streams_and_drops_empty_rows(self, tmp_path):
        path = tmp_path / "cfg.xlsx"
        _write_xlsx(
            path,
            ["protocol_type", "code", "source_ip"],
            [
                ["modbus_tcp", "T1", "10.0.0.1"],
                [None, None, None],              # fully empty -> dropped
                ["modbus_tcp", "T2", "10.0.0.2"],
            ],
        )
        svc = ExcelImportService(job=None, excel_path=path)
        df = svc.load_dataframe()
        assert list(df.columns) == ["protocol_type", "code", "source_ip"]
        assert len(df) == 2
        assert df.iloc[0]["code"] == "T1"
        assert df.iloc[1]["code"] == "T2"

    def test_load_dataframe_missing_file_raises(self, tmp_path):
        svc = ExcelImportService(job=None, excel_path=tmp_path / "nope.xlsx")
        with pytest.raises(FileNotFoundError):
            svc.load_dataframe()

    @pytest.mark.django_db
    def test_apply_persists_all_rows_across_chunk_boundaries(self, tmp_path):
        # 150 rows > the 100-row chunk size: exercises the chunk boundary in
        # both the device pass and the point pass, and the cross-chunk
        # device_cache reuse.
        from configuration import models as cfg_models

        n = 150
        path = tmp_path / "big.xlsx"
        _write_xlsx(
            path,
            ["protocol_type", "source_ip", "source_port", "code", "address"],
            [["modbus_tcp", "192.168.50.10", 502, f"PT_{i:03d}", str(40001 + i)]
             for i in range(n)],
        )
        job = cfg_models.ImportJob.objects.create(
            source_name="big.xlsx", triggered_by="test", status="pending", summary={},
        )
        result = ExcelImportService(job, path).apply(
            site_code="xiu2_chunk_site", mode="merge",
        )
        assert result["point_created"] == n
        device = cfg_models.Device.objects.get(site__code="xiu2_chunk_site")
        assert device.points.count() == n


# ===========================================================================
# C6 — WebSocket session-status query collapse + cache
# ===========================================================================

class TestC6SessionStatus:
    def test_sqlite_options_present_in_settings(self):
        settings_src = (
            Path(__file__).resolve().parent.parent
            / "control_plane" / "settings.py"
        ).read_text()
        assert '"timeout": 30' in settings_src
        assert '"check_same_thread": False' in settings_src

    @pytest.mark.django_db(transaction=True)
    def test_get_session_status_counts_and_caches(self, create_session):
        from asgiref.sync import async_to_sync
        from django.utils import timezone

        from acquisition import models as acq_models
        from acquisition.consumers import AcquisitionConsumer, _status_cache

        _status_cache.clear()
        session = create_session()
        now = timezone.now()
        for i in range(5):
            acq_models.DataPoint.objects.create(
                session=session, point_code="p", timestamp=now, value=i, quality="good",
            )
        for i in range(2):
            acq_models.DataPoint.objects.create(
                session=session, point_code="p", timestamp=now, value=i, quality="bad",
            )

        consumer = AcquisitionConsumer()
        consumer.session_id = session.id

        first = async_to_sync(consumer.get_session_status)()
        assert first["points_read"] == 7
        assert first["error_count"] == 2
        assert first["last_read_time"] is not None

        # Within the TTL a repeat call is served from cache (same object).
        cached = async_to_sync(consumer.get_session_status)()
        assert cached is first

        # After the cache is cleared a fresh payload is computed.
        _status_cache.clear()
        fresh = async_to_sync(consumer.get_session_status)()
        assert fresh is not first
        assert fresh["points_read"] == 7
