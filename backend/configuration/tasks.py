"""Celery tasks for configuration workflows."""
from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from configuration import import_paths, models
from configuration.services import importer

logger = logging.getLogger(__name__)


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def process_excel_import(self, job_id: int, excel_path: str, site_code: str | None = None) -> dict:
    """Validate Excel configuration asynchronously."""

    logger.info("开始处理 Excel 导入任务 job_id=%s", job_id)
    with transaction.atomic():
        job = models.ImportJob.objects.select_for_update().get(pk=job_id)
        summary = importer.process_excel(job, Path(excel_path), site_code=site_code)
    logger.info("完成 Excel 导入任务 job_id=%s status=%s", job_id, job.status)
    return summary.to_dict()


@shared_task
def cleanup_import_jobs(retention_days: int | None = None) -> dict:
    """M12: periodically purge stale import jobs and their uploaded Excel files.

    Without this, every Excel import leaves an ``ImportJob`` row and an
    ``uploads/import_jobs/<id>_<name>.xlsx`` file behind forever. This task —
    run on a daily Celery beat schedule (see ``CELERY_BEAT_SCHEDULE``) —
    deletes jobs older than ``retention_days`` and removes their files.

    The applied configuration itself is preserved independently as
    ``ConfigVersion`` snapshots, so dropping old ImportJob rows loses no
    config history.
    """
    if retention_days is None:
        retention_days = getattr(settings, "IMPORT_JOB_RETENTION_DAYS", 30)

    cutoff = timezone.now() - timedelta(days=retention_days)
    stale = models.ImportJob.objects.filter(created_at__lt=cutoff)

    removed_files = 0
    job_ids: list[int] = []
    for job in stale.iterator():
        job_ids.append(job.id)
        file_path = (job.summary or {}).get("file_path")
        if not file_path:
            continue
        try:
            path = import_paths.resolve(file_path)
            if path.exists():
                path.unlink()
                removed_files += 1
        except OSError as exc:  # noqa: BLE001 - best-effort file cleanup
            logger.warning(
                "cleanup_import_jobs: failed to remove file %s: %s", file_path, exc
            )

    deleted_jobs, _ = stale.delete()
    logger.info(
        "cleanup_import_jobs: removed %d job(s) and %d file(s) older than %d days",
        deleted_jobs, removed_files, retention_days,
    )
    return {
        "deleted_jobs": deleted_jobs,
        "removed_files": removed_files,
        "retention_days": retention_days,
        "job_ids": job_ids,
    }
