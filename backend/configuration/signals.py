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
    """删设备时,连带删掉绑定这台设备的所有采集任务。

    约定:一个采集任务最多绑一台设备(见 AcqTaskSerializer.validate)。所以删设备
    时,凡是引用了它测点的任务都属于它,全部删掉 —— 否则删了设备、测点被级联清掉,
    任务却留下变成空壳。

    (历史遗留的跨设备任务在这个规则下也会被删:删其中任一设备就删掉整个任务。这与
    「删设备删掉其所有任务」的语义一致,且新数据不会再产生跨设备任务。)
    """
    task_ids = sorted(
        models.AcqTask.objects
        .filter(points__device_id=instance.pk)
        .values_list("id", flat=True)
        .distinct()
    )
    if task_ids:
        # delete() 的第一个返回值是含级联表(TaskPoint 绑定行、会话等)的总行数,
        # 不是任务数 —— 日志里报它会虚高误导排查,按 task_ids 计。
        _, per_model = models.AcqTask.objects.filter(id__in=task_ids).delete()
        logger.info(
            "Cascade-deleted %d task(s) bound to device %s: %s",
            per_model.get(models.AcqTask._meta.label, len(task_ids)),
            instance.code, task_ids,
        )
