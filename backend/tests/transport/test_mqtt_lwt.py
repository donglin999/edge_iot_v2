"""Center LWT presence handler — Phase 2 P4 (XIU-103).

Tests for :mod:`fleet.presence`, which folds inbound ``edge/<id>/lwt``
frames into :class:`fleet.models.EdgeNode.status`. This replaces the
WS-heartbeat-driven presence detection in :mod:`fleet.consumers` (whose
heartbeat / disconnect-side paths now live in :mod:`fleet._legacy`).

Coverage:

* ``state: online`` flips an existing edge to ONLINE + stamps last_seen.
* ``state: offline`` flips an existing edge to OFFLINE and writes one
  ``session.offline`` :class:`EdgeLifecycleEvent` row (parity with the
  retired WS-disconnect synthesis path).
* An unknown edge name is dropped (no row auto-provisioning).
* A malformed payload (no/garbage ``state``) is dropped without
  regressing the edge's status.
* The router-level wiring: a frame stamped ``type: "lwt"`` on the
  default router lands in the presence handler.
"""
from __future__ import annotations

import pytest
from django.utils import timezone

from fleet import presence
from fleet.models import EdgeLifecycleEvent, EdgeNode, EdgeStatus
from fleet.uplink_router import LWT_FRAME_TYPE, UplinkRouter


pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _patch_db_defaults():
    """Mirror test_fleet.py: re-inject the connection defaults the session-scoped
    db-config fixture in conftest drops, so ``database_sync_to_async`` thread
    workers don't KeyError on missing connection keys."""
    from django.conf import settings

    db = settings.DATABASES["default"]
    db.setdefault("TIME_ZONE", None)
    db.setdefault("CONN_HEALTH_CHECKS", False)
    db.setdefault("CONN_MAX_AGE", 0)
    db.setdefault("AUTOCOMMIT", True)
    db.setdefault("OPTIONS", {})
    yield


@pytest.mark.asyncio
async def test_online_flips_status_and_refreshes_last_seen():
    from asgiref.sync import sync_to_async

    edge, _ = await sync_to_async(EdgeNode.issue)(name="edge-online")
    assert edge.status == EdgeStatus.PENDING
    assert edge.last_seen is None

    await presence.apply_lwt("edge-online", {
        "type": "lwt", "state": "online", "edge_id": "edge-online",
        "ts": "2026-05-28T00:00:00Z",
    })

    refreshed = await sync_to_async(EdgeNode.objects.get)(name="edge-online")
    assert refreshed.status == EdgeStatus.ONLINE
    assert refreshed.last_seen is not None
    # last_seen must be a recent wall-clock — within a few seconds of now.
    delta = timezone.now() - refreshed.last_seen
    assert delta.total_seconds() < 5


@pytest.mark.asyncio
async def test_offline_flips_status_and_records_session_offline():
    from asgiref.sync import sync_to_async

    edge, _ = await sync_to_async(EdgeNode.issue)(name="edge-offline")
    # Mark online first so the OFFLINE transition is observable.
    await sync_to_async(edge.mark_online)(version="0.1.0")

    await presence.apply_lwt("edge-offline", {
        "type": "lwt", "state": "offline", "edge_id": "edge-offline",
        "ts": "2026-05-28T00:00:00Z",
    })

    refreshed = await sync_to_async(EdgeNode.objects.get)(name="edge-offline")
    assert refreshed.status == EdgeStatus.OFFLINE

    # Exactly one session.offline lifecycle row recorded for this edge.
    rows = await sync_to_async(
        lambda: list(EdgeLifecycleEvent.objects.filter(
            edge=refreshed, event="session.offline"
        ))
    )()
    assert len(rows) == 1
    assert rows[0].monotonic_seq == 0  # center-synthesised


@pytest.mark.asyncio
async def test_offline_for_already_offline_edge_still_logs_event():
    """A flapping broker reconnect must keep the lifecycle timeline complete."""
    from asgiref.sync import sync_to_async

    edge, _ = await sync_to_async(EdgeNode.issue)(name="edge-flap")
    await sync_to_async(
        lambda: EdgeNode.objects.filter(pk=edge.pk).update(status=EdgeStatus.OFFLINE)
    )()

    await presence.apply_lwt("edge-flap", {"type": "lwt", "state": "offline"})

    rows = await sync_to_async(
        lambda: list(EdgeLifecycleEvent.objects.filter(
            edge_id=edge.pk, event="session.offline"
        ))
    )()
    assert len(rows) == 1  # the new row is always written


@pytest.mark.asyncio
async def test_unknown_edge_is_dropped_silently():
    """No EdgeNode row is auto-provisioned from an LWT payload."""
    from asgiref.sync import sync_to_async

    # Should not raise; the handler logs + drops.
    await presence.apply_lwt("ghost-edge", {"type": "lwt", "state": "online"})
    await presence.apply_lwt("ghost-edge", {"type": "lwt", "state": "offline"})

    # No row with that name was auto-provisioned.
    assert not await sync_to_async(
        EdgeNode.objects.filter(name="ghost-edge").exists
    )()


@pytest.mark.asyncio
async def test_malformed_state_does_not_regress_status():
    """A missing / unknown ``state`` must not regress an already-online edge."""
    from asgiref.sync import sync_to_async

    edge, _ = await sync_to_async(EdgeNode.issue)(name="edge-stable")
    await sync_to_async(edge.mark_online)(version="0.1.0")

    # Each of these is an invalid state — none should alter status.
    for bad in ({"type": "lwt"},
                {"type": "lwt", "state": ""},
                {"type": "lwt", "state": "BOGUS"},
                {"type": "lwt", "state": None}):
        await presence.apply_lwt("edge-stable", bad)

    refreshed = await sync_to_async(EdgeNode.objects.get)(name="edge-stable")
    assert refreshed.status == EdgeStatus.ONLINE


@pytest.mark.asyncio
async def test_install_binds_handler_on_router():
    """:func:`install` makes the router route ``lwt`` frames to ``apply_lwt``."""
    from asgiref.sync import sync_to_async

    edge, _ = await sync_to_async(EdgeNode.issue)(name="router-edge")

    router = UplinkRouter()
    presence.install(router)

    # No fallback handler set — dispatching a non-lwt frame must log+drop;
    # dispatching an ``lwt`` frame must reach apply_lwt.
    await router.dispatch("router-edge", {"type": "lwt", "state": "online"})
    refreshed = await sync_to_async(EdgeNode.objects.get)(name="router-edge")
    assert refreshed.status == EdgeStatus.ONLINE


@pytest.mark.asyncio
async def test_router_type_handler_takes_precedence_over_fallback():
    """A type-specific handler is preferred over the generic fallback.

    The router has to keep the existing log+drop fallback available for
    not-yet-wired frame types (lifecycle / sample_batch / alarm_event)
    without short-circuiting the LWT-specific binding.
    """
    seen_fallback: list = []
    seen_lwt: list = []

    async def fallback(edge, frame):
        seen_fallback.append((edge, frame))

    async def lwt_handler(edge, frame):
        seen_lwt.append((edge, frame))

    router = UplinkRouter()
    router.set_handler(fallback)
    router.set_type_handler(LWT_FRAME_TYPE, lwt_handler)

    await router.dispatch("e", {"type": "lwt", "state": "online"})
    await router.dispatch("e", {"type": "lifecycle", "event": "x"})

    assert len(seen_lwt) == 1
    assert seen_lwt[0][1]["type"] == "lwt"
    assert len(seen_fallback) == 1
    assert seen_fallback[0][1]["type"] == "lifecycle"


@pytest.mark.asyncio
async def test_router_unbind_type_handler():
    """``set_type_handler(t, None)`` clears the binding and falls back."""
    seen_fallback: list = []
    seen_lwt: list = []

    async def fallback(edge, frame):
        seen_fallback.append((edge, frame))

    async def lwt_handler(edge, frame):
        seen_lwt.append((edge, frame))

    router = UplinkRouter()
    router.set_handler(fallback)
    router.set_type_handler(LWT_FRAME_TYPE, lwt_handler)
    router.set_type_handler(LWT_FRAME_TYPE, None)

    await router.dispatch("e", {"type": "lwt", "state": "online"})
    assert seen_lwt == []
    assert len(seen_fallback) == 1
