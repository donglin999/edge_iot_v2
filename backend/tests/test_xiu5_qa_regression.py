"""XIU-5 QA regression tests — Django/Celery API robustness fixes.

Covers the four regression points the backend engineer flagged for QA that
were not otherwise exercised by the existing suite:

* H8 — ``start_acquisition_task`` idempotency guard (no double session on a
  redelivered / racing dispatch).
* H9 — ``test-connection`` returns 504 (not a hung WSGI thread) when the
  Celery probe times out.
* M1 — ``_update_session_locked`` re-reads the row so a request-handler
  metadata write does not clobber the acquisition loop's concurrent writes.
* M2 — ``delete_owned_tasks`` deletes device-orphaned tasks but keeps tasks
  that span multiple devices.
* M3 — the new ``Point.code`` / ``Alarm(point_code, status)`` indexes exist
  in the database schema.
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest
from celery.exceptions import TimeoutError as CeleryTimeoutError
from django.db import connection
from rest_framework.test import APIClient

from acquisition import models as acq_models
from acquisition.tasks import start_acquisition_task
from acquisition.views import _update_session_locked
from configuration import models as config_models
from configuration.signals import delete_owned_tasks
from tests.fixtures.factories import *  # noqa: F401,F403 - pytest fixtures


# --------------------------------------------------------------------------
# H8 — idempotency guard on start_acquisition_task
# --------------------------------------------------------------------------
@pytest.mark.django_db
class TestH8IdempotencyGuard:
    def test_duplicate_dispatch_skips_when_session_already_running(
        self, create_task, create_session
    ):
        """A second invocation must not spawn a 2nd pipeline/session."""
        task = create_task(is_active=True)
        running = create_session(
            task=task, status=acq_models.AcquisitionSession.STATUS_RUNNING
        )

        result = start_acquisition_task(task.id)

        assert result["status"] == "skipped"
        assert result["reason"] == "already running"
        assert result["session_id"] == running.id
        # No second session row was created.
        assert (
            acq_models.AcquisitionSession.objects.filter(task=task).count() == 1
        )

    @patch("acquisition.tasks.AcquisitionService")
    def test_dispatch_proceeds_when_no_running_session(
        self, mock_service_class, create_task
    ):
        """With no RUNNING session the guard is a no-op and the task runs."""
        task = create_task(is_active=True)
        mock_service = MagicMock()
        mock_service.run_continuous.return_value = {"status": "completed"}
        mock_service_class.return_value = mock_service

        result = start_acquisition_task(task.id)

        assert result["status"] != "skipped"
        assert acq_models.AcquisitionSession.objects.filter(task=task).exists()


# --------------------------------------------------------------------------
# H9 — test-connection no longer blocks the WSGI thread
# --------------------------------------------------------------------------
@pytest.mark.django_db
class TestH9TestConnectionTimeout:
    def _device(self, create_device):
        return create_device(protocol="mock_modbus")

    def test_timeout_returns_504_not_a_hung_thread(self, create_device):
        """When the Celery probe exceeds 5s the view returns 504 promptly."""
        device = self._device(create_device)

        fake_async = MagicMock()
        fake_async.id = "fake-task-id"
        fake_async.get.side_effect = CeleryTimeoutError("probe timed out")

        with patch(
            "acquisition.tasks.check_protocol_connection.apply_async",
            return_value=fake_async,
        ):
            resp = APIClient().post(
                f"/api/config/devices/{device.id}/test-connection/"
            )

        assert resp.status_code == 504
        assert resp.data["success"] is False
        assert resp.data["details"]["status"] == "timeout"

    def test_success_path_returns_200(self, create_device):
        """A healthy probe result still yields a normal 200 response."""
        device = self._device(create_device)

        fake_async = MagicMock()
        fake_async.id = "fake-task-id"
        fake_async.get.return_value = {
            "status": "success",
            "connected": True,
            "healthy": True,
        }

        with patch(
            "acquisition.tasks.check_protocol_connection.apply_async",
            return_value=fake_async,
        ):
            resp = APIClient().post(
                f"/api/config/devices/{device.id}/test-connection/"
            )

        assert resp.status_code == 200
        assert resp.data["success"] is True


# --------------------------------------------------------------------------
# M1 — _update_session_locked re-reads the row (no clobber)
# --------------------------------------------------------------------------
@pytest.mark.django_db
class TestM1SessionMetadataLock:
    def test_helper_rereads_metadata_so_concurrent_writes_survive(
        self, create_session
    ):
        """A stale in-memory session must not erase the loop's DB writes."""
        session = create_session(metadata={})

        # Simulate the acquisition loop writing a live counter straight to DB
        # after the request handler already loaded a (now stale) instance.
        acq_models.AcquisitionSession.objects.filter(pk=session.pk).update(
            metadata={"last_read_time": "loop-write"}
        )
        # `session` in memory still has metadata == {}.

        updated = _update_session_locked(
            session,
            metadata_mutator=lambda m: m.__setitem__("stopped_by", "qa"),
            field_updates={
                "status": acq_models.AcquisitionSession.STATUS_STOPPED
            },
        )

        # The loop's write is preserved AND the handler's write is applied.
        assert updated.metadata["last_read_time"] == "loop-write"
        assert updated.metadata["stopped_by"] == "qa"
        assert updated.status == acq_models.AcquisitionSession.STATUS_STOPPED

        session.refresh_from_db()
        assert session.metadata == {
            "last_read_time": "loop-write",
            "stopped_by": "qa",
        }


# --------------------------------------------------------------------------
# M2 — delete_owned_tasks keeps multi-device tasks
# --------------------------------------------------------------------------
@pytest.mark.django_db
class TestM2DeleteOwnedTasks:
    def test_orphan_task_deleted_multidevice_task_kept(
        self, create_site, create_device, create_point
    ):
        """Deleting a device drops its solo tasks, keeps shared ones."""
        site = create_site()
        device_a = create_device(site=site)
        device_b = create_device(site=site)

        point_a = create_point(device=device_a, code="PT_A")
        point_b = create_point(device=device_b, code="PT_B")

        # Task bound only to device_a -> orphaned when device_a goes away.
        solo = config_models.AcqTask.objects.create(code="SOLO", name="solo")
        solo.points.set([point_a])

        # Task spanning device_a and device_b -> must survive.
        shared = config_models.AcqTask.objects.create(
            code="SHARED", name="shared"
        )
        shared.points.set([point_a, point_b])

        device_a.delete()  # fires post_delete -> delete_owned_tasks

        assert not config_models.AcqTask.objects.filter(pk=solo.pk).exists()
        assert config_models.AcqTask.objects.filter(pk=shared.pk).exists()

    def test_query_count_is_constant_not_n_plus_1(
        self, create_site, create_device, create_point
    ):
        """delete_owned_tasks resolves orphans in a constant # of queries."""
        site = create_site()
        device = create_device(site=site)
        # 5 tasks, each solely on this device.
        for i in range(5):
            pt = create_point(device=device, code=f"PT_{i}")
            t = config_models.AcqTask.objects.create(
                code=f"T_{i}", name=f"t{i}"
            )
            t.points.set([pt])

        # delete_owned_tasks itself must not scale with the task count: it
        # issues 2 set queries + the cascade delete (no per-task .exists()).
        counter = _QueryCounter()
        with connection.execute_wrapper(counter):
            delete_owned_tasks(sender=config_models.Device, instance=device)
        # 2 lookups + delete machinery; an N+1 would add one .exists() per
        # task (5+ extra). Allow headroom for the cascade delete statements.
        # Distributed-M3 (XIU-63) added two SET_NULL FKs to AcqTask
        # (EdgeLifecycleEvent.task / EdgeSample.task), so the cascade now
        # issues two extra bulk UPDATE statements. That cost is *constant*
        # (verified flat at n=5/30/100), so the "not N+1" guarantee still
        # holds — the threshold is bumped 10 -> 14 to absorb the new FKs.
        assert counter.count <= 14, f"too many queries: {counter.count}"


class _QueryCounter:
    def __init__(self):
        self.count = 0

    def __call__(self, execute, sql, params, many, context):
        self.count += 1
        return execute(sql, params, many, context)


# --------------------------------------------------------------------------
# M3 — new indexes exist in the schema
# --------------------------------------------------------------------------
@pytest.mark.django_db
class TestM3Indexes:
    def test_point_code_index_present(self):
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(
                cursor, config_models.Point._meta.db_table
            )
        assert "point_code_idx" in constraints
        assert constraints["point_code_idx"]["columns"] == ["code"]

    def test_alarm_point_status_index_present(self):
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(
                cursor, acq_models.Alarm._meta.db_table
            )
        assert "alarm_point_status_idx" in constraints
        assert constraints["alarm_point_status_idx"]["columns"] == [
            "point_code",
            "status",
        ]
