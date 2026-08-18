"""Phase 2b — connectivity failures persisted as alarms.

The pipeline's :class:`ReadWorker` already emits *ephemeral* WebSocket
lifecycle events (``gave_up`` / ``reconnecting`` / ``reconnected`` /
``connected``). Those are lost when no browser is watching. This suite
verifies the ADDED behaviour: on the healthy->down transition the worker
raises a persisted ``connectivity`` alarm via the phase-1 reporting helper,
and clears it on recovery — without regressing the WS emits.

The worker is driven SYNCHRONOUSLY in the test thread (``worker.run()``,
not ``worker.start()``) so every ORM write happens inside the test's own
DB transaction — no cross-thread SQLite surprises. A stub protocol whose
``connect`` fails-then-recovers stands in for real device I/O.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from acquisition import models as acq_models
from acquisition.services import pipeline as pipeline_mod
from acquisition.services import reporting
from acquisition.services.pipeline import ReadWorker

# pytest fixtures (create_session, ...)
from tests.fixtures.factories import *  # noqa: F401,F403


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeWS:
    """Captures ``_emit`` payloads so we can assert the WS events still fire.

    Duck-types the one method ``ReadWorker._emit`` calls on its sink
    (``consume_event``); the worker only checks ``self._ws_sink is None``, so
    assigning an instance post-construction is enough.
    """

    def __init__(self) -> None:
        self.events: list[dict] = []

    def consume_event(self, payload: dict) -> None:
        self.events.append(payload)

    def types(self) -> list[str]:
        return [e["event"] for e in self.events]


class _FlakyProto:
    """A protocol stub whose ``connect`` fails ``fail_connects`` times then
    succeeds. ``read_batch`` is a no-op (empty read plan)."""

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


def _make_worker(*, session=None, max_reconnect=1, backoff=0.0, device_code="DEV-A"):
    device = SimpleNamespace(
        id=1,
        code=device_code,
        protocol="generic",  # non-modbus -> empty read plan
        metadata={},
        ip_address="127.0.0.1",
        port=502,
    )
    worker = ReadWorker(
        device=device,
        points=[],
        sinks=[],
        sample_rate_hz=100.0,  # tiny cycle interval
        shutdown_event=threading.Event(),
        health_dict={},
        max_reconnect=max_reconnect,
        reconnect_backoff=backoff,
        session=session,
    )
    worker._ws_sink = _FakeWS()
    return worker


def _install_proto(monkeypatch, proto):
    """Force ``_connect`` to always build the same stub protocol instance."""
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
# Offline -> alarm raised (one-shot)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOfflineRaisesAlarm:
    def test_crossing_max_reconnect_raises_one_firing_alarm(self, monkeypatch):
        worker = _make_worker(device_code="PLC-OFF", max_reconnect=1)
        _install_proto(monkeypatch, _FlakyProto(fail_connects=9999))  # never recovers

        # Break out of the (otherwise infinite) reconnect loop after a few
        # give-up backoff cycles so we exercise repeated downtime cycles.
        sleeps = {"n": 0}

        def _stop_after(_duration):
            sleeps["n"] += 1
            if sleeps["n"] >= 3:
                worker.shutdown_event.set()
                return True
            return False

        worker._interruptible_sleep = _stop_after

        worker.run()

        # Exactly ONE firing connectivity alarm despite 3 give-up cycles.
        qs = _firing_connectivity("connectivity:PLC-OFF")
        assert qs.count() == 1
        alarm = qs.get()
        assert alarm.rule is None
        assert alarm.severity == "critical"
        assert alarm.device_code == "PLC-OFF"
        assert alarm.status == acq_models.Alarm.STATUS_FIRING
        # Value blob carries diagnostics; connect error was captured.
        assert alarm.value["consecutive_failures"] >= 1
        assert "boom" in alarm.value["last_error"]
        # One-shot flag is set.
        assert worker._offline_alarm_raised is True

    def test_continued_failures_do_not_pile_up_rows(self, monkeypatch):
        worker = _make_worker(device_code="PLC-FLAP", max_reconnect=1)
        _install_proto(monkeypatch, _FlakyProto(fail_connects=9999))

        sleeps = {"n": 0}

        def _stop_after(_duration):
            sleeps["n"] += 1
            if sleeps["n"] >= 5:  # many give-up cycles
                worker.shutdown_event.set()
                return True
            return False

        worker._interruptible_sleep = _stop_after
        worker.run()

        # Dedup + one-shot flag => a single row total for this device.
        assert acq_models.Alarm.objects.filter(
            dedup_key="connectivity:PLC-FLAP",
        ).count() == 1

    def test_ws_gave_up_event_still_emitted(self, monkeypatch):
        worker = _make_worker(device_code="PLC-WS", max_reconnect=1)
        _install_proto(monkeypatch, _FlakyProto(fail_connects=9999))

        sleeps = {"n": 0}

        def _stop_after(_duration):
            sleeps["n"] += 1
            if sleeps["n"] >= 2:
                worker.shutdown_event.set()
                return True
            return False

        worker._interruptible_sleep = _stop_after
        worker.run()

        # Persistence is ADDITIVE: the ephemeral WS event is untouched.
        assert "gave_up" in worker._ws_sink.types()


# ---------------------------------------------------------------------------
# Recovery -> alarm cleared
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRecoveryClearsAlarm:
    def _drive_down_then_up(self, monkeypatch, worker, fail_connects):
        proto = _FlakyProto(fail_connects=fail_connects)
        _install_proto(monkeypatch, proto)
        # Stop the loop the first time a read cycle runs — i.e. right after the
        # worker has reconnected. The empty read plan makes this a clean latch.
        def _stop_on_read():
            worker.shutdown_event.set()

        worker._read_cycle = _stop_on_read
        worker.run()
        return proto

    def test_alarm_cleared_after_reconnect(self, monkeypatch):
        worker = _make_worker(device_code="PLC-REC", max_reconnect=1, backoff=0.0)
        # 2 connect failures cross max_reconnect (raise), 3rd connect succeeds.
        self._drive_down_then_up(monkeypatch, worker, fail_connects=2)

        # No firing alarm remains; the single row is now cleared.
        assert _firing_connectivity("connectivity:PLC-REC").count() == 0
        row = acq_models.Alarm.objects.get(dedup_key="connectivity:PLC-REC")
        assert row.status == acq_models.Alarm.STATUS_CLEARED
        assert row.cleared_at is not None
        # One-shot flag re-armed for a future downtime.
        assert worker._offline_alarm_raised is False

    def test_recovery_still_emits_ws_events(self, monkeypatch):
        worker = _make_worker(device_code="PLC-REC2", max_reconnect=1, backoff=0.0)
        self._drive_down_then_up(monkeypatch, worker, fail_connects=2)

        types = worker._ws_sink.types()
        # Down + recovery WS telemetry both still surface.
        assert "gave_up" in types
        assert "connected" in types

    def test_session_linked_on_alarm(self, monkeypatch, create_session):
        session = create_session()
        worker = _make_worker(
            device_code="PLC-SESS", max_reconnect=1, session=session,
        )
        _install_proto(monkeypatch, _FlakyProto(fail_connects=9999))

        sleeps = {"n": 0}

        def _stop_after(_duration):
            sleeps["n"] += 1
            if sleeps["n"] >= 2:
                worker.shutdown_event.set()
                return True
            return False

        worker._interruptible_sleep = _stop_after
        worker.run()

        alarm = _firing_connectivity("connectivity:PLC-SESS").get()
        assert alarm.session_id == session.id


# ---------------------------------------------------------------------------
# Helper-level guard semantics (smallest unit)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOfflineAlarmHelpers:
    def test_raise_is_idempotent_via_flag_and_dedup(self, monkeypatch):
        worker = _make_worker(device_code="U-1")
        _install_proto(monkeypatch, _FlakyProto(fail_connects=0))
        worker._update_health(consecutive_failures=3)

        worker._raise_offline_alarm()
        worker._raise_offline_alarm()  # flag short-circuits the second call
        worker._raise_offline_alarm()

        assert _firing_connectivity("connectivity:U-1").count() == 1
        assert worker._offline_alarm_raised is True

    def test_clear_is_noop_without_outstanding_alarm(self):
        worker = _make_worker(device_code="U-2")
        # No alarm raised yet -> clear must not touch the ORM / must not flip.
        worker._clear_offline_alarm()
        assert worker._offline_alarm_raised is False
        assert acq_models.Alarm.objects.filter(
            dedup_key="connectivity:U-2",
        ).count() == 0

    def test_raise_then_clear_then_raise_again(self, monkeypatch):
        worker = _make_worker(device_code="U-3")
        worker._update_health(consecutive_failures=2)

        worker._raise_offline_alarm()
        worker._clear_offline_alarm()
        worker._raise_offline_alarm()  # re-armed after clear -> new firing row

        assert _firing_connectivity("connectivity:U-3").count() == 1
        # Two distinct rows over time: one cleared, one firing.
        assert acq_models.Alarm.objects.filter(
            dedup_key="connectivity:U-3",
        ).count() == 2

    def test_failed_raise_keeps_guard_open_for_retry(self, monkeypatch):
        worker = _make_worker(device_code="U-4")
        outcomes = iter((None, object()))
        calls = 0

        def flaky_raise(**_kwargs):
            nonlocal calls
            calls += 1
            return next(outcomes)

        monkeypatch.setattr(reporting, "raise_system_alarm", flaky_raise)

        worker._raise_offline_alarm()
        assert worker._offline_alarm_raised is False
        worker._raise_offline_alarm()
        assert worker._offline_alarm_raised is True
        assert calls == 2

    def test_failed_clear_stays_armed_and_retries_after_backoff(self, monkeypatch):
        worker = _make_worker(device_code="U-5")
        worker._offline_alarm_raised = True
        outcomes = iter((None, 1))
        calls = 0

        def flaky_clear(_dedup_key):
            nonlocal calls
            calls += 1
            return next(outcomes)

        now = 100.0
        monkeypatch.setattr(reporting, "clear_system_alarm", flaky_clear)
        monkeypatch.setattr(pipeline_mod.time, "monotonic", lambda: now)

        worker._clear_offline_alarm()
        assert worker._offline_alarm_raised is True
        worker._clear_offline_alarm()
        assert calls == 1

        now = 101.0
        worker._clear_offline_alarm()
        assert worker._offline_alarm_raised is False
        assert calls == 2
