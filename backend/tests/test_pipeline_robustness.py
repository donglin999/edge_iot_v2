"""Verification tests for XIU-4 — acquisition pipeline robustness (H1-H7).

Each test class maps to one of the seven hardening items:

* H1 — ``ReadWorker`` shutdown: interruptible backoff + ``run()`` always
  disconnects + ``Pipeline.stop`` reports workers that did not terminate.
* H2 — ``health_dict`` cross-thread race: health updates are published as
  whole-dict swaps so a reader never sees a half-updated record.
* H3 — ``WebSocketSink`` broadcast drift: the cadence subtracts the work
  time so the push rate does not drift slower than configured.
* H4 — S7 single-point failure no longer discards the whole batch.
* H5 — OPC-UA partial failure is normalised to ``quality="bad"`` readings.
* H6 — overlapping Modbus points reuse the previous read group instead of
  spawning a second, overlapping one.
* H7 — ``WebSocketSink`` splits oversized drains across multiple frames.

The suite avoids the Django ORM and the optional snap7 / asyncua deps by
using duck-typed stand-ins, so it runs fast and in any environment.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from acquisition.protocols import opcua as opcua_mod
from acquisition.protocols import s7 as s7_mod
from acquisition.protocols.base import ReadError
from acquisition.services import sinks as sinks_mod
from acquisition.services.pipeline import AcquisitionPipeline, ReadWorker
from acquisition.services.read_plan import ReadPlanBuilder, Reading
from acquisition.services.sinks import WebSocketSink, _WS_MAX_READINGS_PER_MSG


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_worker(health_dict=None, device_id=1):
    """Build a real ReadWorker with no protocol I/O and an empty read plan."""
    device = SimpleNamespace(
        id=device_id,
        code=f"DEV{device_id}",
        protocol="generic",  # non-modbus -> default (empty) plan
        metadata={},
        ip_address="127.0.0.1",
        port=502,
    )
    return ReadWorker(
        device=device,
        points=[],
        sinks=[],
        sample_rate_hz=10.0,
        shutdown_event=threading.Event(),
        health_dict=health_dict if health_dict is not None else {},
    )


class _StubPoint:
    """Duck-typed Point for ReadPlanBuilder (mirrors test_read_plan stubs)."""

    def __init__(self, code, address, data_type="uint16"):
        self.code = code
        self.address = str(address)
        self.extra = {"data_type": data_type}
        self.template = None


# ---------------------------------------------------------------------------
# H1 — ReadWorker shutdown
# ---------------------------------------------------------------------------


class TestH1WorkerShutdown:
    def test_interruptible_sleep_wakes_immediately_on_shutdown(self):
        worker = _make_worker()
        worker.shutdown_event.set()
        start = time.monotonic()
        interrupted = worker._interruptible_sleep(5.0)
        elapsed = time.monotonic() - start
        assert interrupted is True
        assert elapsed < 0.5  # did not wait the full 5 s

    def test_interruptible_sleep_runs_full_duration_when_not_stopped(self):
        worker = _make_worker()
        start = time.monotonic()
        interrupted = worker._interruptible_sleep(0.3)
        elapsed = time.monotonic() - start
        assert interrupted is False
        assert elapsed >= 0.25

    def test_run_disconnects_protocol_even_when_loop_raises(self):
        """``run()``'s try/finally must close the connection on a crash."""
        worker = _make_worker()
        fake_protocol = MagicMock()
        worker._connect = lambda: None  # skip real connect
        worker.protocol = fake_protocol

        def _boom():
            raise RuntimeError("loop exploded")

        worker._run_loop = _boom

        with pytest.raises(RuntimeError, match="loop exploded"):
            worker.run()

        fake_protocol.disconnect.assert_called_once()

    def test_stop_reports_workers_that_did_not_terminate(self):
        import logging

        from acquisition.services import pipeline as pipeline_mod

        pipeline = AcquisitionPipeline(MagicMock(id=1), sample_rate_hz=1.0)
        stuck = MagicMock(spec=ReadWorker)
        stuck.is_alive.return_value = True
        stuck.name = "ReadWorker-STUCK"
        pipeline.workers = [stuck]
        pipeline.sinks = []

        # The acquisition logger sets propagate=False, so attach our own
        # collecting handler directly rather than relying on caplog/root.
        messages = []

        class _Collector(logging.Handler):
            def emit(self, record):
                messages.append(record.getMessage())

        handler = _Collector()
        pipeline_mod.logger.addHandler(handler)
        try:
            pipeline.stop(timeout=0.01)
        finally:
            pipeline_mod.logger.removeHandler(handler)

        stuck.join.assert_called_once()
        assert any("did not terminate" in m for m in messages)


# ---------------------------------------------------------------------------
# H2 — health_dict cross-thread race
# ---------------------------------------------------------------------------


class TestH2HealthRace:
    def test_update_health_swaps_in_a_fresh_dict(self):
        shared = {}
        worker = _make_worker(health_dict=shared, device_id=7)
        original = worker.health
        snapshot_before = dict(original)

        worker._update_health(status="healthy", consecutive_failures=0)

        # The shared dict the supervisor reads now points at the new record.
        assert shared[7] is worker.health
        assert worker.health["status"] == "healthy"
        # The old record object was NOT mutated — a reader holding it still
        # sees a self-consistent (if stale) snapshot.
        assert original == snapshot_before
        assert worker.health is not original

    def test_update_health_preserves_unchanged_keys(self):
        worker = _make_worker(device_id=3)
        worker._update_health(last_success=123.0)
        worker._update_health(status="error")
        assert worker.health["last_success"] == 123.0
        assert worker.health["status"] == "error"
        assert worker.health["device_code"] == "DEV3"


# ---------------------------------------------------------------------------
# H3 — WebSocketSink broadcast drift
# ---------------------------------------------------------------------------


class _VirtualClock:
    """Deterministic stand-in for ``time`` + a ``threading.Event``.

    Drives ``_broadcast_loop`` with virtual time so the cadence assertion no
    longer depends on wall-clock scheduling (which is preempted under a
    fully-loaded concurrent test run). ``wait`` advances virtual time and
    signals the loop to stop once the simulated horizon is reached.
    """

    def __init__(self, horizon: float) -> None:
        self.now = 0.0
        self._horizon = horizon
        self._stopped = False

    # --- ``time`` surface --------------------------------------------------
    def monotonic(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt

    # --- ``threading.Event`` surface used by ``_broadcast_loop`` -----------
    def is_set(self) -> bool:
        return self._stopped

    def set(self) -> None:
        self._stopped = True

    def wait(self, timeout: float) -> bool:
        self.now += timeout
        if self.now >= self._horizon:
            self._stopped = True
        return self._stopped


class TestH3BroadcastDrift:
    def test_broadcast_cadence_compensates_for_work_time(self, monkeypatch):
        """With a broadcast cost == interval, a naive loop would run at half
        rate. The compensated loop subtracts the work time and keeps pace.

        Driven by a virtual clock so the result is deterministic: no real
        threads, no real sleeps, no dependency on OS scheduling.
        """
        sink = WebSocketSink(MagicMock(id=1), broadcast_interval=0.05)
        # Stop the real loop thread so we can drive a controlled one.
        sink._stop.set()
        sink._thread.join(timeout=1.0)

        # Simulate 0.80 s of virtual time, work cost == interval (0.05 s).
        clock = _VirtualClock(horizon=0.8)
        monkeypatch.setattr(sinks_mod.time, "monotonic", clock.monotonic)
        sink._stop = clock

        calls = []

        def _slow_broadcast():
            calls.append(clock.now)
            clock.advance(0.05)  # work cost == interval

        sink._broadcast_once = _slow_broadcast

        # Runs synchronously; the virtual clock guarantees termination.
        sink._broadcast_loop()

        # Uncompensated period is interval + work = 0.10 s -> 8 calls in
        # 0.80 s. Compensated period is the interval (0.05 s) -> 15 calls.
        # With a virtual clock the count is exact, not a flaky range.
        assert len(calls) == 15
        # And the cadence is the interval, not double it.
        gaps = [b - a for a, b in zip(calls, calls[1:])]
        assert all(abs(g - 0.05) < 1e-9 for g in gaps)


# ---------------------------------------------------------------------------
# H4 — S7 single-point failure isolation
# ---------------------------------------------------------------------------


class TestH4S7PartialFailure:
    def _make_proto(self, monkeypatch, bad_bytes):
        # Map address "N" -> start_byte N so the fake client can fail a
        # specific point; data_type uint16 -> 2-byte read.
        monkeypatch.setattr(
            s7_mod, "_parse_s7_address",
            lambda addr: (0, 0, int(addr), 0, 2),
        )

        class _FakeClient:
            def read_area(self, area, db, byte, length):
                if byte in bad_bytes:
                    raise OSError(f"byte {byte} unreachable")
                return b"\x00\x2a"  # uint16 -> 42

        proto = s7_mod.SiemensS7Protocol({"source_ip": "127.0.0.1"})
        proto.is_connected = True
        proto.client = _FakeClient()
        return proto

    def test_one_bad_point_does_not_void_the_batch(self, monkeypatch):
        proto = self._make_proto(monkeypatch, bad_bytes={2})
        points = [
            {"code": "p1", "address": "1", "data_type": "uint16"},
            {"code": "p2", "address": "2", "data_type": "uint16"},
            {"code": "p3", "address": "3", "data_type": "uint16"},
        ]
        results = proto.read_points(points)

        by_code = {r["code"]: r for r in results}
        assert by_code["p1"]["quality"] == "good"
        assert by_code["p1"]["value"] == 42
        assert by_code["p2"]["quality"] == "bad"
        assert by_code["p2"]["value"] is None
        assert by_code["p3"]["quality"] == "good"

    def test_all_points_failing_raises_for_reconnect(self, monkeypatch):
        proto = self._make_proto(monkeypatch, bad_bytes={1, 2})
        points = [
            {"code": "p1", "address": "1", "data_type": "uint16"},
            {"code": "p2", "address": "2", "data_type": "uint16"},
        ]
        with pytest.raises(ReadError, match="all 2 point"):
            proto.read_points(points)


# ---------------------------------------------------------------------------
# H5 — OPC-UA partial failure normalisation
# ---------------------------------------------------------------------------


class _FakeRunner:
    """Stand-in for OPCUAProtocol._runner — runs no event loop."""

    def __init__(self, outcomes):
        self.outcomes = outcomes

    def run(self, coro):
        coro.close()  # we never await it; avoids "never awaited" warning
        return self.outcomes


class TestH5OpcuaPartialFailure:
    def _make_proto(self, outcomes):
        proto = opcua_mod.OPCUAProtocol({"endpoint_url": "opc.tcp://h:4840"})
        proto.is_connected = True
        proto._runner = _FakeRunner(outcomes)
        return proto

    def test_bad_node_becomes_bad_quality_reading(self):
        proto = self._make_proto([(11, None), (None, "BadNodeIdUnknown")])
        points = [
            {"code": "a", "address": "ns=2;s=A"},
            {"code": "b", "address": "ns=2;s=B"},
        ]
        results = proto.read_points(points)
        by_code = {r["code"]: r for r in results}
        assert by_code["a"]["quality"] == "good"
        assert by_code["a"]["value"] == 11
        assert by_code["b"]["quality"] == "bad"
        assert by_code["b"]["value"] is None

    def test_all_nodes_failing_raises_for_reconnect(self):
        proto = self._make_proto([(None, "err1"), (None, "err2")])
        points = [
            {"code": "a", "address": "ns=2;s=A"},
            {"code": "b", "address": "ns=2;s=B"},
        ]
        with pytest.raises(ReadError, match="all 2 point"):
            proto.read_points(points)


# ---------------------------------------------------------------------------
# H6 — read_plan overlapping points
# ---------------------------------------------------------------------------


class TestH6OverlapPlan:
    def test_overlapping_point_reuses_previous_group(self):
        """A point starting inside an already-covered range must fold into
        the current group, never open a second overlapping group."""
        device = SimpleNamespace(protocol="modbus_tcp", metadata={})
        points = [
            _StubPoint("wide", 0, data_type="int64"),   # addr 0, spans 0..3
            _StubPoint("inner", 2, data_type="uint16"),  # addr 2 -> inside 0..3
        ]
        groups = ReadPlanBuilder.build(device, points, gap_threshold=0)

        assert len(groups) == 1
        g = groups[0]
        assert g.start_address == 0
        assert g.register_count == 4  # unchanged — inner point adds nothing
        assert {p.code for p in g.points} == {"wide", "inner"}

    def test_overlap_extends_group_without_splitting(self):
        device = SimpleNamespace(protocol="modbus_tcp", metadata={})
        points = [
            _StubPoint("a", 0, data_type="int32"),   # 0..1
            _StubPoint("b", 1, data_type="int32"),   # 1..2 -> overlaps a
        ]
        groups = ReadPlanBuilder.build(device, points, gap_threshold=0)

        assert len(groups) == 1
        g = groups[0]
        assert g.start_address == 0
        assert g.register_count == 3  # extended to cover addr 1..2


# ---------------------------------------------------------------------------
# H7 — WebSocketSink payload size cap
# ---------------------------------------------------------------------------


class _FakeChannelLayer:
    def __init__(self):
        self.sent = []

    async def group_send(self, group, message):
        self.sent.append((group, message))


class TestH7PayloadCap:
    def _make_sink(self):
        sink = WebSocketSink(MagicMock(id=1), broadcast_interval=1.0)
        sink._stop.set()
        sink._thread.join(timeout=1.0)
        sink._channel_layer = _FakeChannelLayer()
        return sink

    def test_oversized_drain_is_split_into_multiple_frames(self):
        sink = self._make_sink()
        n = _WS_MAX_READINGS_PER_MSG * 2 + 5  # forces 3 chunks
        with sink._lock:
            sink._buffer = [
                Reading(point_code=f"pt{i}", value=i, timestamp_ns=1, quality="good")
                for i in range(n)
            ]

        sink._broadcast_once()

        # 3 chunks x 2 groups (session + global) = 6 group_send calls.
        layer = sink._channel_layer
        assert len(layer.sent) == 6
        per_group = [m for g, m in layer.sent if g == "acquisition_session_1"]
        assert len(per_group) == 3
        sizes = [len(m["data"]["readings"]) for m in per_group]
        assert sizes == [_WS_MAX_READINGS_PER_MSG, _WS_MAX_READINGS_PER_MSG, 5]
        assert all(m["data"]["chunk_count"] == 3 for m in per_group)
        assert sorted(m["data"]["chunk_index"] for m in per_group) == [0, 1, 2]

    def test_small_drain_is_a_single_frame(self):
        sink = self._make_sink()
        with sink._lock:
            sink._buffer = [
                Reading(point_code=f"pt{i}", value=i, timestamp_ns=1, quality="good")
                for i in range(10)
            ]

        sink._broadcast_once()

        layer = sink._channel_layer
        assert len(layer.sent) == 2  # 1 chunk x 2 groups
        for _g, m in layer.sent:
            assert m["data"]["chunk_count"] == 1
            assert len(m["data"]["readings"]) == 10
