"""Center-side LWT presence handler — Phase 2 P4 (XIU-103).

The edge publishes ``edge/<id>/lwt`` retained=True on every successful
connect (``{state: online, ts}``); the broker auto-publishes the same
topic with the configured WILL payload (``{state: offline, ts}``) on
any ungraceful disconnect (TCP RST, edge crash, keepalive timeout).
The MQTT subscriber (``fleet.mqtt_transport.run_subscriber``) reads
both and dispatches them through :data:`fleet.uplink_router.default_router`
with ``frame.type = "lwt"``.

:func:`apply_lwt` is the type-specific router handler. It folds the
presence state into :class:`fleet.models.EdgeNode`:

* ``state == "online"``  → ``status=ONLINE``, ``last_seen=now``
* ``state == "offline"`` → ``status=OFFLINE``; also synthesises a
  ``session.offline`` ``EdgeLifecycleEvent`` so the operator's lifecycle
  timeline shows the disconnect (parity with the WS-disconnect path the
  consumer used to own).

A frame for an unknown edge name is logged and dropped — we do not
auto-provision edges from LWT alone; an unregistered edge is an
operator misconfiguration that should not silently create rows.

Bind once from :meth:`fleet.apps.FleetConfig.ready` so the handler is
in place before the subscriber thread starts consuming.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from channels.db import database_sync_to_async
from django.utils import timezone

from .models import EdgeLifecycleEvent, EdgeNode, EdgeStatus
from .protocol import LIFECYCLE_SESSION_OFFLINE
from .uplink_router import LWT_FRAME_TYPE, UplinkRouter, default_router

logger = logging.getLogger(__name__)


LWT_STATE_ONLINE = "online"
LWT_STATE_OFFLINE = "offline"


@database_sync_to_async
def _apply_online(edge_name: str) -> bool:
    """Sync DB write: flip the edge to online, refresh ``last_seen``.

    Returns True on a successful update, False if the edge is unknown.
    """
    now = timezone.now()
    updated = (
        EdgeNode.objects.filter(name=edge_name)
        .update(status=EdgeStatus.ONLINE, last_seen=now, updated_at=now)
    )
    return updated > 0


@database_sync_to_async
def _apply_offline(edge_name: str) -> bool:
    """Sync DB write: flip the edge to offline + record session.offline.

    Returns True on a successful update, False if the edge is unknown.
    The lifecycle row carries ``monotonic_seq=0`` (center-synthesised,
    matching the original WS-disconnect synthesis path in the consumer).
    """
    now = timezone.now()
    edge = EdgeNode.objects.filter(name=edge_name).first()
    if edge is None:
        return False
    # Skip the no-op write on an already-offline edge — keeps
    # ``updated_at`` stable across a flapping broker reconnect.
    if edge.status != EdgeStatus.OFFLINE:
        EdgeNode.objects.filter(pk=edge.pk).update(
            status=EdgeStatus.OFFLINE, updated_at=now,
        )
    EdgeLifecycleEvent.objects.create(
        edge_id=edge.pk,
        event=LIFECYCLE_SESSION_OFFLINE,
        monotonic_seq=0,
        received_at=now,
    )
    return True


async def apply_lwt(edge_name: str, frame: Dict[str, Any]) -> None:
    """Router handler for ``type == "lwt"`` frames (P4 — XIU-103)."""
    state = str(frame.get("state") or "").lower()
    if state == LWT_STATE_ONLINE:
        ok = await _apply_online(edge_name)
        if not ok:
            logger.warning(
                "fleet.presence: lwt online for unknown edge=%r — dropping",
                edge_name,
            )
            return
        logger.info("fleet.presence: edge=%s online (lwt)", edge_name)
    elif state == LWT_STATE_OFFLINE:
        ok = await _apply_offline(edge_name)
        if not ok:
            logger.warning(
                "fleet.presence: lwt offline for unknown edge=%r — dropping",
                edge_name,
            )
            return
        logger.info("fleet.presence: edge=%s offline (lwt)", edge_name)
    else:
        # An unknown state lands here — drop it without touching status.
        # ``frame["state"]`` is the edge-supplied payload field, so a
        # malformed payload must NOT regress an edge to ``pending`` or
        # similar; the existing status is the safer default.
        logger.warning(
            "fleet.presence: edge=%s ignoring lwt with unknown state=%r",
            edge_name, frame.get("state"),
        )


def install(router: UplinkRouter | None = None) -> None:
    """Bind :func:`apply_lwt` as the ``lwt`` handler on ``router``.

    Idempotent — re-installing simply rebinds the same handler. Called
    from :meth:`fleet.apps.FleetConfig.ready` so the handler is in
    place before the MQTT subscriber thread connects.
    """
    target = router or default_router
    target.set_type_handler(LWT_FRAME_TYPE, apply_lwt)
