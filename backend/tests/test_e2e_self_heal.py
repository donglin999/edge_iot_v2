"""End-to-end integration for self-healing + anomaly-reporting (拉通).

This module is the *拉通* test: it drives the FULL acquisition chain as one
story and asserts the three feature goals are actually wired together, rather
than re-testing units already covered by ``test_pipeline*.py`` /
``test_self_heal.py`` / ``test_connectivity_alarms.py`` / ``test_reporting.py``.

Three stories, mapped to the three goals:

1. **全流程拉通 (happy path)** — a real Device/Task/Point config is grouped by
   :class:`AcquisitionService`, a :class:`ReadWorker` reads it through a mock
   protocol, and the decoded readings land in the sink layer. The session
   heartbeat (``metadata.last_health_update``) then advances through the real
   health-writer.

2. **异常上报 (connectivity)** — the same style of worker is driven into
   repeated connection failure past ``max_reconnect``; a *persisted*
   ``connectivity`` Alarm proves the anomaly is durable with no browser
   watching (the worker has no ``WebSocketSink``). Recovery clears it.

3. **故障自起 (self-restart)** — a RUNNING session with a stale heartbeat is
   handed to ``watchdog_recover_sessions()`` (with ``start_acquisition_task``
   dispatch mocked), which re-dispatches with ``resume_session_id`` and bumps
   ``restart_count`` on the SAME row; driving it past the cap flips the session
   to ERROR with a persisted ``system``/critical alarm — and never clones the
   session row.

Everything runs synchronously in the test thread (``worker.run()`` / eager
Celery) so every ORM write stays inside the test transaction — no real
threads, Redis, or InfluxDB.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from acquisition import models as acq_models
from acquisition import tasks as acq_tasks
from acquisition.services import pipeline as pipeline_mod
from acquisition.services import restart_policy
from acquisition.services.acquisition_service import AcquisitionService
from acquisition.services.pipeline import AcquisitionPipeline, ReadWorker
from acquisition.services.sinks import Sink

from tests.mocks.protocols import register_mock_protocols

# pytest fixtures (create_task, create_device, create_point, create_session, …)
from tests.fixtures.factories import *  # noqa: F401,F403


pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def _mock_protocols():
    """Make ``mock_modbus`` resolvable through the real ProtocolRegistry."""
    register_mock_protocols()


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class CaptureSink(Sink):
    """Minimal sink that records every Reading fanned out to it.

    Stands in for the InfluxDB/WebSocket/Alarm sinks so the happy path can
    assert data reached the sink layer without any real I/O.
    """

    def __init__(self) -> None:
        self.readings = []

    def consume(self, reading) -> None:
        self.readings.append(reading)


class _FlakyProto:
    """Protocol stub whose ``connect`` fails ``fail_connects`` times then
    succeeds (mirrors the fail-then-recover stub in
    ``test_connectivity_alarms.py``). ``read_batch`` is a no-op empty plan."""

    def __init__(self, fail_connects: int) -> None:
        self._fail_connects = fail_connects
        self.connect_calls = 0
        self.is_connected = False

    def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_calls <= self._fail_connects:
            self.is_connected = False
            raise ConnectionError(f"boom #{self.connect_calls}")
        self.is_connected = True

    def read_batch(self, group):  # pragma: no cover - empty plan, never called
        return []

    def disconnect(self) -> None:
        self.is_connected = False


def _install_proto(monkeypatch, proto):
    """Force ``ProtocolRegistry.create`` to hand back ``proto`` every time."""
    monkeypatch.setattr(
        pipeline_mod.ProtocolRegistry, "create",
        classmethod(lambda cls, protocol_type, cfg: proto),
    )


def _firing_connectivity(dedup_key):
    return acq_models.Alarm.objects.filter(
        category="connectivity",
        dedup_key=dedup_key,
        status=acq_models.Alarm.STATUS_FIRING,
    )


# ---------------------------------------------------------------------------
# Goal 1 — 全流程拉通 (device -> read -> sink, heartbeat advances)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestHappyPathFullChain:
    def test_readings_reach_sink_and_session_heartbeat_advances(
        self, create_device, create_point, create_task, create_session,
    ):
        # --- build a real config: one device, three points, one task -------
        simulated = {"E2E_A": 11, "E2E_B": 22, "E2E_C": 33}
        device = create_device(
            code="E2E-DEV",
            protocol="mock_modbus",
            metadata={"_test_simulated_data": simulated},
        )
        points = [
            create_point(device=device, code=code, address="D100")
            for code in simulated
        ]
        task = create_task(points=points)
        stale_hb = time.time() - 1000.0
        session = create_session(
            task=task, metadata={"last_health_update": stale_hb},
        )

        # AcquisitionService performs the real point-grouping the pipeline
        # consumes — exercise that instead of hand-building point dicts.
        service = AcquisitionService(task, session)
        group = service.device_groups[device.id]

        # --- drive ONE real read cycle through the sink layer --------------
        capture = CaptureSink()
        worker = ReadWorker(
            device=group["device"],
            points=group["points"],
            sinks=[capture],
            sample_rate_hz=float(task.sample_rate_hz),
            shutdown_event=threading.Event(),
            health_dict={},
            session=session,
        )
        # Let the real _read_cycle run exactly once, then stop the loop.
        real_read_cycle = worker._read_cycle

        def _read_once():
            real_read_cycle()
            worker.shutdown_event.set()

        worker._read_cycle = _read_once
        worker.run()

        # The whole device -> read -> sink chain produced data.
        assert capture.readings, "no readings reached the sink layer"
        got = {r.point_code: r.value for r in capture.readings}
        assert got == simulated
        assert all(r.quality == "good" for r in capture.readings)

        # The worker's health record shows a successful read.
        health = worker._health_dict[device.id]
        assert health["status"] == "healthy"
        assert health["last_success"] is not None

        # --- heartbeat advances through the real health-writer -------------
        # Reuse the health the worker just produced; no InfluxDB sink is
        # attached (pipeline never .start()ed), so the writer only touches
        # SQLite metadata.
        pipeline = AcquisitionPipeline(session, sample_rate_hz=1.0)
        pipeline.health = worker._health_dict
        service.pipeline = pipeline
        service._update_session_health(pipeline, force_sqlite=True)

        session.refresh_from_db()
        assert session.metadata["last_health_update"] > stale_hb
        assert device.code in session.metadata["device_health"]
        assert session.metadata["device_health"][device.code]["status"] == "healthy"


# ---------------------------------------------------------------------------
# Goal 2 — 异常上报 (durable connectivity alarm, cleared on recovery)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestConnectivityAnomalyReporting:
    def _make_worker(self, session, *, max_reconnect=1):
        device = SimpleNamespace(
            id=1,
            code="E2E-OFF",
            protocol="generic",  # non-modbus -> empty read plan
            metadata={},
            ip_address="127.0.0.1",
            port=502,
        )
        worker = ReadWorker(
            device=device,
            points=[],
            sinks=[],  # NO WebSocketSink -> "no browser watching"
            sample_rate_hz=100.0,
            shutdown_event=threading.Event(),
            health_dict={},
            max_reconnect=max_reconnect,
            reconnect_backoff=0.0,
            session=session,
        )
        return worker

    def test_offline_persists_alarm_then_recovery_clears_it(
        self, monkeypatch, create_session,
    ):
        session = create_session()

        # --- phase A: device is down, never recovers ----------------------
        down = self._make_worker(session)
        _install_proto(monkeypatch, _FlakyProto(fail_connects=9999))
        sleeps = {"n": 0}

        def _stop_after(_duration):
            sleeps["n"] += 1
            if sleeps["n"] >= 3:
                down.shutdown_event.set()
                return True
            return False

        down._interruptible_sleep = _stop_after
        down.run()

        # A durable connectivity alarm exists even though no WS sink watched.
        qs = _firing_connectivity("connectivity:E2E-OFF")
        assert qs.count() == 1
        alarm = qs.get()
        assert alarm.severity == "critical"
        assert alarm.device_code == "E2E-OFF"
        assert alarm.rule is None
        assert alarm.session_id == session.id  # scoped to the owning session
        assert alarm.value["consecutive_failures"] >= 1
        assert "boom" in alarm.value["last_error"]

        # --- phase B: device recovers -> alarm clears ---------------------
        up = self._make_worker(session)
        # 2 connect failures cross max_reconnect(1) (re-raise), 3rd succeeds.
        _install_proto(monkeypatch, _FlakyProto(fail_connects=2))

        def _stop_on_read():
            up.shutdown_event.set()

        up._read_cycle = _stop_on_read
        up.run()

        assert _firing_connectivity("connectivity:E2E-OFF").count() == 0
        row = acq_models.Alarm.objects.get(dedup_key="connectivity:E2E-OFF")
        assert row.status == acq_models.Alarm.STATUS_CLEARED
        assert row.cleared_at is not None


# ---------------------------------------------------------------------------
# Goal 3 — 故障自起 (watchdog restart -> escalation, no duplicate session)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSelfRestartEndToEnd:
    def _stale_running(self, create_session):
        return create_session(
            status=acq_models.AcquisitionSession.STATUS_RUNNING,
            metadata={"last_health_update": time.time() - 120.0},
        )

    def test_watchdog_restarts_then_escalates_without_cloning_session(
        self, create_session,
    ):
        session = self._stale_running(create_session)

        with patch("acquisition.tasks.start_acquisition_task.delay") as delay:
            # --- first watchdog tick: the dead session is re-dispatched ----
            result = acq_tasks.watchdog_recover_sessions()
            assert result["restarted"] == 1
            delay.assert_called_once_with(
                session.task_id, None, resume_session_id=session.id,
            )

            session.refresh_from_db()
            assert session.metadata["restart_count"] == 1
            # Still RUNNING — the SAME row is adopted, never cloned.
            assert session.status == acq_models.AcquisitionSession.STATUS_RUNNING
            assert acq_models.AcquisitionSession.objects.count() == 1

            # --- drive to the cap: bypass backoff, keep heartbeat stale ----
            for _ in range(restart_policy.MAX_AUTO_RESTARTS - 1):
                session.refresh_from_db()
                meta = dict(session.metadata)
                meta["last_restart_at"] = time.time() - 100000.0
                meta["last_health_update"] = time.time() - 120.0
                session.metadata = meta
                session.save(update_fields=["metadata"])
                acq_tasks.watchdog_recover_sessions()

            session.refresh_from_db()
            assert session.metadata["restart_count"] == restart_policy.MAX_AUTO_RESTARTS
            # dispatched once per successful restart, and no more.
            assert delay.call_count == restart_policy.MAX_AUTO_RESTARTS

            # --- one more tick past the cap: escalate to ERROR + alarm -----
            meta = dict(session.metadata)
            meta["last_restart_at"] = time.time() - 100000.0
            meta["last_health_update"] = time.time() - 120.0
            session.metadata = meta
            session.save(update_fields=["metadata"])

            escalate = acq_tasks.watchdog_recover_sessions()
            assert escalate["escalated"] == 1
            # No extra dispatch on the over-budget tick.
            assert delay.call_count == restart_policy.MAX_AUTO_RESTARTS

        session.refresh_from_db()
        assert session.status == acq_models.AcquisitionSession.STATUS_ERROR
        assert session.stopped_at is not None

        # Persisted critical escalation alarm, keyed to this session.
        alarm = acq_models.Alarm.objects.get(
            dedup_key=f"session-restart-failed:{session.id}",
            status=acq_models.Alarm.STATUS_FIRING,
        )
        assert alarm.category == "system"
        assert alarm.severity == "critical"
        assert alarm.value == {"restart_count": restart_policy.MAX_AUTO_RESTARTS}

        # Never a duplicate pipeline/session row across the whole ordeal.
        assert acq_models.AcquisitionSession.objects.count() == 1
