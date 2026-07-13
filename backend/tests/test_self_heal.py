"""Tests for auto-restart / self-healing (phase 2a).

Covers :mod:`acquisition.services.restart_policy`, the
``watchdog_recover_sessions`` Celery task, and the ``resume_session_id``
adoption path in ``start_acquisition_task`` (the no-duplicate-pipeline guard).

``start_acquisition_task.delay`` is mocked throughout so restarts are asserted,
never actually spawned.
"""
import time
from unittest.mock import MagicMock, patch

import pytest

from acquisition import models as acq_models
from acquisition import tasks as acq_tasks
from acquisition.services import restart_policy

# pytest fixtures (create_session, create_task, …)
from tests.fixtures.factories import *  # noqa: F401,F403


NOW = time.time()


def _stale_meta(age=120.0, **extra):
    return {"last_health_update": NOW - age, **extra}


def _fresh_meta(age=5.0, **extra):
    return {"last_health_update": NOW - age, **extra}


@pytest.mark.django_db
class TestAttemptRestart:
    def test_stale_under_cap_redispatches_and_increments(self, create_session):
        session = create_session(
            status=acq_models.AcquisitionSession.STATUS_RUNNING,
            metadata=_stale_meta(),
        )
        with patch("acquisition.tasks.start_acquisition_task.delay") as delay:
            assert restart_policy.attempt_restart(session) is True
            delay.assert_called_once_with(
                session.task_id, None, resume_session_id=session.id,
            )

        session.refresh_from_db()
        assert session.metadata["restart_count"] == 1
        assert "last_restart_at" in session.metadata
        assert session.metadata["restart_history"][-1]["count"] == 1
        # Still RUNNING — the same row is reused by the adopting task.
        assert session.status == acq_models.AcquisitionSession.STATUS_RUNNING

    def test_config_version_forwarded_from_metadata(self, create_session):
        session = create_session(
            status=acq_models.AcquisitionSession.STATUS_RUNNING,
            metadata=_stale_meta(config_version_id=77),
        )
        with patch("acquisition.tasks.start_acquisition_task.delay") as delay:
            restart_policy.attempt_restart(session)
            delay.assert_called_once_with(
                session.task_id, 77, resume_session_id=session.id,
            )

    def test_backoff_blocks_second_immediate_call(self, create_session):
        session = create_session(
            status=acq_models.AcquisitionSession.STATUS_RUNNING,
            metadata=_stale_meta(),
        )
        with patch("acquisition.tasks.start_acquisition_task.delay") as delay:
            assert restart_policy.attempt_restart(session) is True
            # Second call immediately after — inside the backoff window.
            assert restart_policy.attempt_restart(session) is False
            delay.assert_called_once()  # NOT re-dispatched

        session.refresh_from_db()
        assert session.metadata["restart_count"] == 1  # unchanged

    def test_restart_again_after_backoff_elapsed(self, create_session):
        # 1 restart already done, but last_restart_at is far in the past.
        session = create_session(
            status=acq_models.AcquisitionSession.STATUS_RUNNING,
            metadata=_stale_meta(restart_count=1, last_restart_at=NOW - 100000),
        )
        with patch("acquisition.tasks.start_acquisition_task.delay") as delay:
            assert restart_policy.attempt_restart(session) is True
            delay.assert_called_once()

        session.refresh_from_db()
        assert session.metadata["restart_count"] == 2

    def test_cap_exceeded_marks_error_and_raises_alarm(self, create_session):
        session = create_session(
            status=acq_models.AcquisitionSession.STATUS_RUNNING,
            metadata=_stale_meta(restart_count=restart_policy.MAX_AUTO_RESTARTS),
        )
        with patch("acquisition.tasks.start_acquisition_task.delay") as delay:
            assert restart_policy.attempt_restart(session) is False
            delay.assert_not_called()

        session.refresh_from_db()
        assert session.status == acq_models.AcquisitionSession.STATUS_ERROR
        assert session.stopped_at is not None

        alarm = acq_models.Alarm.objects.filter(
            dedup_key=f"session-restart-failed:{session.id}",
            status=acq_models.Alarm.STATUS_FIRING,
        ).first()
        assert alarm is not None
        assert alarm.category == "system"
        assert alarm.severity == "critical"
        assert alarm.value == {"restart_count": restart_policy.MAX_AUTO_RESTARTS}

    def test_cap_alarm_is_deduped(self, create_session):
        """Escalating twice for the same session yields a single firing alarm."""
        session = create_session(
            status=acq_models.AcquisitionSession.STATUS_RUNNING,
            metadata=_stale_meta(restart_count=restart_policy.MAX_AUTO_RESTARTS),
        )
        restart_policy.attempt_restart(session)
        # Reset to RUNNING and escalate again — dedup_key must collapse them.
        session.status = acq_models.AcquisitionSession.STATUS_RUNNING
        session.save(update_fields=["status"])
        restart_policy.attempt_restart(session)

        firing = acq_models.Alarm.objects.filter(
            dedup_key=f"session-restart-failed:{session.id}",
            status=acq_models.Alarm.STATUS_FIRING,
        )
        assert firing.count() == 1


@pytest.mark.django_db
class TestRecordRecovery:
    def test_resets_count_and_clears_alarm(self, create_session):
        session = create_session(
            status=acq_models.AcquisitionSession.STATUS_RUNNING,
            metadata=_fresh_meta(restart_count=3, last_restart_at=NOW - 5),
        )
        # A firing escalation alarm from an earlier bad spell.
        from acquisition.services.reporting import raise_system_alarm
        raise_system_alarm(
            category="system",
            severity="critical",
            message="old failure",
            dedup_key=f"session-restart-failed:{session.id}",
        )

        restart_policy.record_recovery(session)

        session.refresh_from_db()
        assert session.metadata["restart_count"] == 0
        assert "last_restart_at" not in session.metadata

        assert not acq_models.Alarm.objects.filter(
            dedup_key=f"session-restart-failed:{session.id}",
            status=acq_models.Alarm.STATUS_FIRING,
        ).exists()

    def test_no_write_when_budget_already_clean(self, create_session):
        session = create_session(
            status=acq_models.AcquisitionSession.STATUS_RUNNING,
            metadata=_fresh_meta(),
        )
        # Should not raise and should leave count absent/zero.
        restart_policy.record_recovery(session)
        session.refresh_from_db()
        assert session.metadata.get("restart_count", 0) == 0


@pytest.mark.django_db
class TestWatchdogTask:
    def test_stale_session_restarted(self, create_session):
        session = create_session(
            status=acq_models.AcquisitionSession.STATUS_RUNNING,
            metadata=_stale_meta(),
        )
        with patch("acquisition.tasks.start_acquisition_task.delay") as delay:
            result = acq_tasks.watchdog_recover_sessions()
            delay.assert_called_once_with(
                session.task_id, None, resume_session_id=session.id,
            )

        assert result["restarted"] == 1
        session.refresh_from_db()
        assert session.metadata["restart_count"] == 1

    def test_fresh_session_not_restarted(self, create_session):
        session = create_session(
            status=acq_models.AcquisitionSession.STATUS_RUNNING,
            metadata=_fresh_meta(),
        )
        with patch("acquisition.tasks.start_acquisition_task.delay") as delay:
            result = acq_tasks.watchdog_recover_sessions()
            delay.assert_not_called()

        assert result["restarted"] == 0
        session.refresh_from_db()
        assert session.status == acq_models.AcquisitionSession.STATUS_RUNNING

    def test_brand_new_session_in_grace_not_restarted(self, create_session):
        # No heartbeat yet, started just now → inside the startup grace window.
        session = create_session(
            status=acq_models.AcquisitionSession.STATUS_RUNNING,
            metadata={},
        )
        with patch("acquisition.tasks.start_acquisition_task.delay") as delay:
            result = acq_tasks.watchdog_recover_sessions()
            delay.assert_not_called()
        assert result["restarted"] == 0

    def test_fresh_session_resets_budget(self, create_session):
        session = create_session(
            status=acq_models.AcquisitionSession.STATUS_RUNNING,
            metadata=_fresh_meta(restart_count=2, last_restart_at=NOW - 3),
        )
        with patch("acquisition.tasks.start_acquisition_task.delay"):
            acq_tasks.watchdog_recover_sessions()
        session.refresh_from_db()
        assert session.metadata["restart_count"] == 0

    def test_stopped_session_ignored(self, create_session):
        session = create_session(
            status=acq_models.AcquisitionSession.STATUS_STOPPED,
            metadata=_stale_meta(),
        )
        with patch("acquisition.tasks.start_acquisition_task.delay") as delay:
            result = acq_tasks.watchdog_recover_sessions()
            delay.assert_not_called()
        assert result["scanned"] == 0


@pytest.mark.django_db
class TestNoDuplicatePipeline:
    """The resume_session_id adoption path must reuse the row, not clone it."""

    @patch("acquisition.tasks.AcquisitionService")
    def test_resume_adopts_existing_session(self, mock_service_class, create_task):
        task = create_task(is_active=True)
        mock_service = MagicMock()
        mock_service.run_continuous.return_value = {"status": "completed", "cycles": 1}
        mock_service_class.return_value = mock_service

        stale = acq_models.AcquisitionSession.objects.create(
            task=task,
            status=acq_models.AcquisitionSession.STATUS_RUNNING,
            celery_task_id="dead-worker",
            metadata=_stale_meta(restart_count=2),
        )

        # Run synchronously (eager) with resume pointing at the stale session.
        acq_tasks.start_acquisition_task(
            task.id, None, resume_session_id=stale.id,
        )

        # No second session was created — the same row was adopted.
        assert acq_models.AcquisitionSession.objects.filter(task=task).count() == 1
        stale.refresh_from_db()
        # restart bookkeeping survived the adoption.
        assert stale.metadata["restart_count"] == 2

    def test_live_duplicate_still_skipped(self, create_task):
        task = create_task(is_active=True)
        running = acq_models.AcquisitionSession.objects.create(
            task=task,
            status=acq_models.AcquisitionSession.STATUS_RUNNING,
            celery_task_id="live-worker",
            metadata=_fresh_meta(),
        )
        # resume_session_id points at a DIFFERENT id → not an adoption → skip.
        result = acq_tasks.start_acquisition_task(
            task.id, None, resume_session_id=running.id + 9999,
        )
        assert result["status"] == "skipped"
        assert result["reason"] == "already running"
        assert acq_models.AcquisitionSession.objects.filter(task=task).count() == 1
