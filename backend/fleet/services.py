"""Helper services for the fleet app."""
from __future__ import annotations

from django.db.models import Q
from django.utils import timezone

from .models import OFFLINE_AFTER, EdgeNode, EdgeStatus


def sweep_stale_edges(now=None) -> int:
    """Flip any edge whose last_seen is older than OFFLINE_AFTER to offline.

    Returns the number of rows updated. Cheap to call inline from the
    list endpoint — it's a single UPDATE bounded by an index on `status`.
    """
    now = now or timezone.now()
    cutoff = now - OFFLINE_AFTER
    return (
        EdgeNode.objects.filter(status=EdgeStatus.ONLINE)
        .filter(Q(last_seen__lt=cutoff) | Q(last_seen__isnull=True))
        .update(status=EdgeStatus.OFFLINE, updated_at=now)
    )
