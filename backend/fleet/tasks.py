"""Celery tasks for the center-side edge fleet (XIU-66)."""
from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from fleet.models import EdgeLifecycleEvent

logger = logging.getLogger(__name__)

# How many rows to delete per query. ``EdgeLifecycleEvent`` can grow large
# for a flapping edge, and SQLite — the default relational store — holds a
# write lock for the duration of a DELETE. Chunking keeps each transaction
# short so the prune never stalls the real-time uplink ingest path.
_DELETE_BATCH_SIZE = 5000


@shared_task
def cleanup_edge_lifecycle_events(retention_days: int | None = None) -> dict:
    """XIU-66: periodically purge stale ``EdgeLifecycleEvent`` audit rows.

    ``EdgeLifecycleEvent`` is append-only — every inbound ``lifecycle`` frame
    and every center-synthesised ``session.offline`` writes a row. Unlike
    ``EdgeSample`` (bounded by ``unique_together``) and the InfluxDB sample
    mirror (bounded by bucket retention), the lifecycle table has no natural
    ceiling, so a flapping edge would grow it without bound.

    Run on a daily Celery beat schedule (see ``CELERY_BEAT_SCHEDULE``), this
    task deletes rows whose ``received_at`` is older than ``retention_days``.
    The window is configurable via ``EDGE_LIFECYCLE_EVENT_RETENTION_DAYS``;
    a non-positive value disables pruning entirely.

    Deletion is chunked (:data:`_DELETE_BATCH_SIZE` rows per query) so a large
    backlog cannot hold a long write lock and stall the uplink ingest path.
    """
    if retention_days is None:
        retention_days = getattr(settings, "EDGE_LIFECYCLE_EVENT_RETENTION_DAYS", 7)
    retention_days = int(retention_days)

    if retention_days <= 0:
        logger.info(
            "cleanup_edge_lifecycle_events: pruning disabled (retention_days=%d)",
            retention_days,
        )
        return {"deleted": 0, "retention_days": retention_days, "disabled": True}

    cutoff = timezone.now() - timedelta(days=retention_days)
    deleted_total = 0
    while True:
        batch_ids = list(
            EdgeLifecycleEvent.objects.filter(received_at__lt=cutoff)
            .values_list("id", flat=True)
            .order_by("id")[:_DELETE_BATCH_SIZE]
        )
        if not batch_ids:
            break
        deleted, _ = EdgeLifecycleEvent.objects.filter(id__in=batch_ids).delete()
        deleted_total += deleted
        if len(batch_ids) < _DELETE_BATCH_SIZE:
            break

    logger.info(
        "cleanup_edge_lifecycle_events: removed %d lifecycle row(s) older than %d days",
        deleted_total,
        retention_days,
    )
    return {
        "deleted": deleted_total,
        "retention_days": retention_days,
        "disabled": False,
    }
