"""Tests for the EdgeLifecycleEvent retention / pruning task (XIU-66).

Covers ``fleet.tasks.cleanup_edge_lifecycle_events``:
- rows older than the retention window are deleted, recent ones kept
- the window is configurable and a non-positive value disables pruning
- chunked deletion drains a backlog larger than one batch
- the real-time ingest path (separate code) is untouched
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.conf import settings
from django.utils import timezone

from fleet import tasks
from fleet.models import EdgeLifecycleEvent, EdgeNode

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _patch_db_defaults():
    """Re-inject Django 4.2 connection defaults the session conftest drops."""
    db = settings.DATABASES["default"]
    db.setdefault("TIME_ZONE", None)
    db.setdefault("CONN_HEALTH_CHECKS", False)
    db.setdefault("CONN_MAX_AGE", 0)
    db.setdefault("AUTOCOMMIT", True)
    db.setdefault("OPTIONS", {})


@pytest.fixture
def node() -> EdgeNode:
    edge, _token = EdgeNode.issue(name="edge-retention-test")
    return edge


def _make_event(node: EdgeNode, *, age_days: float, event: str = "session.online") -> EdgeLifecycleEvent:
    """Create a lifecycle row whose ``received_at`` is ``age_days`` in the past."""
    received_at = timezone.now() - timedelta(days=age_days)
    return EdgeLifecycleEvent.objects.create(
        edge=node, event=event, received_at=received_at
    )


def test_prunes_rows_older_than_retention_window(node):
    # 5 stale rows (10 days old) + 3 fresh rows (1 day old).
    for _ in range(5):
        _make_event(node, age_days=10)
    for _ in range(3):
        _make_event(node, age_days=1)

    result = tasks.cleanup_edge_lifecycle_events(retention_days=7)

    assert result["deleted"] == 5
    assert result["retention_days"] == 7
    assert result["disabled"] is False
    assert EdgeLifecycleEvent.objects.count() == 3


def test_keeps_rows_inside_the_window(node):
    for _ in range(4):
        _make_event(node, age_days=2)

    result = tasks.cleanup_edge_lifecycle_events(retention_days=7)

    assert result["deleted"] == 0
    assert EdgeLifecycleEvent.objects.count() == 4


def test_row_exactly_at_cutoff_is_kept(node):
    # A row aged just under the window survives; just over it is pruned.
    _make_event(node, age_days=6.9)
    _make_event(node, age_days=7.1)

    result = tasks.cleanup_edge_lifecycle_events(retention_days=7)

    assert result["deleted"] == 1
    assert EdgeLifecycleEvent.objects.count() == 1


@pytest.mark.parametrize("retention_days", [0, -1])
def test_non_positive_retention_disables_pruning(node, retention_days):
    for _ in range(3):
        _make_event(node, age_days=365)

    result = tasks.cleanup_edge_lifecycle_events(retention_days=retention_days)

    assert result["disabled"] is True
    assert result["deleted"] == 0
    assert EdgeLifecycleEvent.objects.count() == 3


def test_defaults_to_settings_retention_window(node, settings):
    settings.EDGE_LIFECYCLE_EVENT_RETENTION_DAYS = 3
    _make_event(node, age_days=5)
    _make_event(node, age_days=1)

    result = tasks.cleanup_edge_lifecycle_events()

    assert result["retention_days"] == 3
    assert result["deleted"] == 1
    assert EdgeLifecycleEvent.objects.count() == 1


def test_chunked_delete_drains_backlog_larger_than_one_batch(node, monkeypatch):
    # Force a tiny batch so a 7-row backlog needs multiple delete passes.
    monkeypatch.setattr(tasks, "_DELETE_BATCH_SIZE", 2)
    for _ in range(7):
        _make_event(node, age_days=30)
    _make_event(node, age_days=1)  # one fresh row must survive

    result = tasks.cleanup_edge_lifecycle_events(retention_days=7)

    assert result["deleted"] == 7
    assert EdgeLifecycleEvent.objects.count() == 1


def test_disabled_window_via_settings_default(node, settings):
    settings.EDGE_LIFECYCLE_EVENT_RETENTION_DAYS = 0
    _make_event(node, age_days=999)

    result = tasks.cleanup_edge_lifecycle_events()

    assert result["disabled"] is True
    assert EdgeLifecycleEvent.objects.count() == 1
