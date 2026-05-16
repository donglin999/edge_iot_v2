"""Signal handlers that keep device/task data consistent."""
from __future__ import annotations

import logging

from django.db.models.signals import post_save, pre_delete, pre_save
from django.dispatch import receiver

from . import models

logger = logging.getLogger(__name__)


@receiver(pre_save, sender=models.AcqTask)
def _track_sample_rate_change(sender, instance, **kwargs):
    """Stash the previous ``sample_rate_hz`` so the post_save can decide.

    Sets ``instance._sample_rate_changed = True`` only when the row already
    existed and the rate is being modified. New rows always get
    ``False`` — there's no running session to restart.
    """
    if not instance.pk:
        instance._sample_rate_changed = False
        return
    try:
        old = sender.objects.only("sample_rate_hz").get(pk=instance.pk)
    except sender.DoesNotExist:
        instance._sample_rate_changed = False
        return
    instance._sample_rate_changed = old.sample_rate_hz != instance.sample_rate_hz


@receiver(post_save, sender=models.AcqTask)
def _restart_on_rate_change(sender, instance, created, **kwargs):
    """If the sample rate changed and a session is running, stop+start it.

    The acquisition celery worker uses ``--pool=solo`` and ``start_acquisition_task``
    is a long-running task — that means a queued ``stop_acquisition_task`` would
    block behind the runner and never execute. We bypass the queue by:

    1. Updating the session row to STOPPED — the running pipeline polls the
       DB every cycle and exits gracefully when it sees the new status.
    2. Directly revoking the celery task (``terminate=True``) so the worker
       slot is freed.
    3. Re-queueing ``start_acquisition_task`` with a 3 s countdown so the old
       worker has time to flush sinks and disconnect the protocol.
    """
    if created or not getattr(instance, "_sample_rate_changed", False):
        return
    try:
        from django.utils import timezone

        from acquisition import models as acq_models
        from acquisition import tasks as acq_tasks
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cannot import acquisition tasks for restart: %s", exc)
        return

    running = acq_models.AcquisitionSession.objects.filter(
        task=instance,
        status=acq_models.AcquisitionSession.STATUS_RUNNING,
    ).first()
    if not running:
        return

    logger.info(
        "Sample rate changed on task %s; restarting session %s with new rate %s Hz",
        instance.code, running.id, instance.sample_rate_hz,
    )

    # 1) Flip session status — the running pipeline observes this on its
    #    next ``_should_continue`` check and exits gracefully.
    running.status = acq_models.AcquisitionSession.STATUS_STOPPED
    running.stopped_at = timezone.now()
    try:
        running.save(update_fields=["status", "stopped_at", "updated_at"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to mark session %s stopped: %s", running.id, exc)
        return

    # 2) Revoke the celery task so the solo-pool worker slot is freed.
    if running.celery_task_id:
        try:
            from celery import current_app

            current_app.control.revoke(running.celery_task_id, terminate=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Revoke for celery task %s failed: %s",
                running.celery_task_id, exc,
            )

    # 3) Schedule the new acquisition task — countdown gives the old worker
    #    time to flush sinks and close protocol connections.
    try:
        acq_tasks.start_acquisition_task.apply_async(args=[instance.id], countdown=3)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to schedule restart for task %s: %s", instance.code, exc)


@receiver(pre_delete, sender=models.Device)
def delete_owned_tasks(sender, instance: models.Device, **kwargs) -> None:
    """Delete acquisition tasks whose points all live on the device being deleted.

    Without this, deleting a device would cascade-delete its points, but the
    auto-imported task that referenced those points would remain as an empty
    "ghost" task in the UI. We only delete tasks that are exclusively bound
    to this device — tasks that span multiple devices are kept intact.
    """
    # M2: resolve orphans with two set queries instead of an N+1 ``.exists()``
    # loop. ``candidate`` = tasks referencing this device; ``multi_device`` =
    # the subset that also has at least one point on a *different* device.
    # Orphans = candidate − multi_device.
    candidate_task_ids = set(
        models.AcqTask.objects
        .filter(points__device_id=instance.pk)
        .values_list("id", flat=True)
        .distinct()
    )

    multi_device_task_ids = set(
        models.TaskPoint.objects
        .filter(task_id__in=candidate_task_ids)
        .exclude(point__device_id=instance.pk)
        .values_list("task_id", flat=True)
        .distinct()
    )

    orphan_ids = sorted(candidate_task_ids - multi_device_task_ids)

    if orphan_ids:
        deleted, _ = models.AcqTask.objects.filter(id__in=orphan_ids).delete()
        logger.info(
            "Cascade-deleted %d orphan task(s) for device %s: %s",
            deleted, instance.code, orphan_ids,
        )
