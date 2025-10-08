"""Celery tasks for configuration workflows."""
from __future__ import annotations

import logging
from pathlib import Path

from celery import shared_task
from django.db import transaction

from configuration import models
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
