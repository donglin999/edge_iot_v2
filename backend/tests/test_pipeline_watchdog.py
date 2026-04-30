"""Unit tests for the AcquisitionPipeline / AcquisitionService watchdog.

Verify two pieces of the lifecycle plumbing added alongside the
``total_points_read`` recovery:

1. :meth:`AcquisitionPipeline.dead_workers` returns workers whose thread
   has terminated (and skips them during ``shutdown_event`` teardown).
2. :meth:`AcquisitionService._supervise_workers` detects a dead worker,
   restarts it, and stops restarting once the per-device crash budget
   is exhausted within ``RESTART_WINDOW_S``.

These tests are intentionally light on Django ORM — we substitute
``MagicMock`` for the session/device wherever we don't need the real
attribute behavior so the tests run fast and don't depend on migrations.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from acquisition.services.acquisition_service import (
    AcquisitionService,
    MAX_RESTARTS_PER_WINDOW,
)
from acquisition.services.pipeline import AcquisitionPipeline, ReadWorker


def _make_dead_worker(device_code: str, device_id: int) -> MagicMock:
    """Build a MagicMock that quacks like a stopped ReadWorker.

    We don't construct a real ReadWorker because doing so triggers
    ReadPlanBuilder.build, protocol creation, etc. The supervisor only
    reaches for: ``device.id``, ``device.code``, ``points``, ``sinks``,
    ``cycle_interval``, ``max_reconnect``, ``connection_timeout``,
    ``reconnect_backoff``, plus ``is_alive()``.
    """
    worker = MagicMock(spec=ReadWorker)
    worker.is_alive.return_value = False
    worker.device = MagicMock()
    worker.device.id = device_id
    worker.device.code = device_code
    worker.points = []
    worker.sinks = []
    worker.cycle_interval = 1.0
    worker.max_reconnect = 3
    worker.connection_timeout = 30.0
    worker.reconnect_backoff = 5.0
    return worker


# ---------------------------------------------------------------------------
# Pipeline-level tests
# ---------------------------------------------------------------------------


class TestPipelineDeadWorkers:
    def test_dead_workers_returns_stopped_threads(self):
        pipeline = AcquisitionPipeline(MagicMock(id=1), sample_rate_hz=1.0)
        alive = MagicMock(spec=ReadWorker)
        alive.is_alive.return_value = True
        dead = MagicMock(spec=ReadWorker)
        dead.is_alive.return_value = False
        pipeline.workers = [alive, dead]

        result = pipeline.dead_workers()

        assert result == [dead]

    def test_dead_workers_empty_during_shutdown(self):
        """During teardown every worker is expected to exit; the watchdog
        must not flag those as crashes."""
        pipeline = AcquisitionPipeline(MagicMock(id=1), sample_rate_hz=1.0)
        dead = MagicMock(spec=ReadWorker)
        dead.is_alive.return_value = False
        pipeline.workers = [dead]
        pipeline.shutdown_event.set()

        assert pipeline.dead_workers() == []

    def test_replace_worker_swaps_in_place_and_starts(self):
        pipeline = AcquisitionPipeline(MagicMock(id=1), sample_rate_hz=1.0)
        old = MagicMock(spec=ReadWorker)
        new = MagicMock(spec=ReadWorker)
        pipeline.workers = [old]

        pipeline.replace_worker(old=old, new=new)

        assert pipeline.workers == [new]
        new.start.assert_called_once()

    def test_replace_worker_appends_when_old_not_tracked(self):
        pipeline = AcquisitionPipeline(MagicMock(id=1), sample_rate_hz=1.0)
        new = MagicMock(spec=ReadWorker)
        pipeline.workers = []

        pipeline.replace_worker(old=MagicMock(spec=ReadWorker), new=new)

        assert pipeline.workers == [new]
        new.start.assert_called_once()


# ---------------------------------------------------------------------------
# Service-level supervisor tests
# ---------------------------------------------------------------------------


def _make_service() -> AcquisitionService:
    """AcquisitionService skeleton without invoking __init__'s ORM access."""
    svc = AcquisitionService.__new__(AcquisitionService)
    svc.task = MagicMock(code="test-task")
    svc.session = MagicMock(id=999)
    svc.logger = MagicMock()
    svc.device_groups = {}
    svc._restart_history = {}
    svc._fatal_devices = set()
    svc._last_influxdb_health_write = 0.0
    svc._last_sqlite_metadata_update = 0.0
    return svc


class TestSupervisorWatchdog:
    def test_dead_worker_triggers_restart(self, monkeypatch):
        """A single dead worker is replaced and the new worker is started."""
        svc = _make_service()
        pipeline = AcquisitionPipeline(MagicMock(id=1), sample_rate_hz=1.0)
        dead = _make_dead_worker("DEV-A", device_id=42)
        pipeline.workers = [dead]
        pipeline.sinks = []  # no WS sink — emit becomes a no-op

        # Stub ReadWorker construction: we don't want a real thread.
        constructed = []

        class FakeWorker:
            def __init__(self, **kwargs):
                constructed.append(kwargs)
                self.kwargs = kwargs

            def start(self):
                self.started = True

        monkeypatch.setattr(
            "acquisition.services.acquisition_service.ReadWorker",
            FakeWorker,
        )

        svc._supervise_workers(pipeline)

        # Old worker swapped for new; new worker started.
        assert len(pipeline.workers) == 1
        assert pipeline.workers[0] is not dead
        assert getattr(pipeline.workers[0], "started", False) is True
        # Restart history recorded.
        assert svc._restart_history[42]["count"] == 1
        # No fatal flag yet.
        assert 42 not in svc._fatal_devices

    def test_restart_budget_exhausted_marks_fatal(self, monkeypatch):
        """After MAX_RESTARTS_PER_WINDOW + 1 crashes the device is fatal."""
        svc = _make_service()
        pipeline = AcquisitionPipeline(MagicMock(id=1), sample_rate_hz=1.0)
        pipeline.sinks = []

        constructed = []

        class FakeWorker:
            def __init__(self, **kwargs):
                constructed.append(kwargs)

            def start(self):
                pass

        monkeypatch.setattr(
            "acquisition.services.acquisition_service.ReadWorker",
            FakeWorker,
        )

        # Simulate MAX + 1 successive deaths.
        for _ in range(MAX_RESTARTS_PER_WINDOW + 1):
            dead = _make_dead_worker("DEV-FLAKY", device_id=7)
            pipeline.workers = [dead]
            svc._supervise_workers(pipeline)

        # Restarts: MAX successful + 1 over-budget call (no construction).
        assert len(constructed) == MAX_RESTARTS_PER_WINDOW
        assert 7 in svc._fatal_devices

        # A subsequent dead-worker tick is a no-op once fatal.
        before = len(constructed)
        dead = _make_dead_worker("DEV-FLAKY", device_id=7)
        pipeline.workers = [dead]
        svc._supervise_workers(pipeline)
        assert len(constructed) == before

    def test_no_dead_workers_is_noop(self):
        svc = _make_service()
        pipeline = AcquisitionPipeline(MagicMock(id=1), sample_rate_hz=1.0)
        alive = MagicMock(spec=ReadWorker)
        alive.is_alive.return_value = True
        pipeline.workers = [alive]
        pipeline.sinks = []

        svc._supervise_workers(pipeline)

        assert svc._restart_history == {}
        assert svc._fatal_devices == set()


# ---------------------------------------------------------------------------
# total_points_read plumbing
# ---------------------------------------------------------------------------


class TestTotalPointsReadPlumbing:
    def test_pipeline_total_points_read_defers_to_sink(self):
        pipeline = AcquisitionPipeline(MagicMock(id=1), sample_rate_hz=1.0)
        # No sink yet -> returns 0.
        assert pipeline.total_points_read == 0

        sink = MagicMock()
        sink.total_written = 123
        pipeline.influx_sink = sink
        assert pipeline.total_points_read == 123

    def test_sink_total_written_starts_at_zero(self):
        from acquisition.services.sinks import InfluxDBSink

        sink = InfluxDBSink.__new__(InfluxDBSink)
        sink._total_written = 0
        assert sink.total_written == 0

        sink._total_written = 17
        assert sink.total_written == 17
