"""Legacy WS-only presence path — preserved for revert (P4 — XIU-103).

Phase 2 P4 retired the WebSocket heartbeat as the source of truth for
``EdgeNode.status`` and replaced it with the retained MQTT LWT topic
(:mod:`fleet.presence`). The handlers that used to own that path live
here so an operator can revert by re-binding them from
:class:`fleet.consumers.FleetConsumer` if the MQTT migration needs to
be unwound mid-flight.

What this module preserves verbatim:

* :func:`legacy_handle_heartbeat` — the original consumer method that
  validated a heartbeat frame, ran the DB ``_touch`` write (status →
  online, last_seen → now, buffer_backlog mirror) and acked back to
  the edge.
* :func:`legacy_record_session_offline` — the disconnect-side synthesis
  that wrote a ``session.offline`` :class:`EdgeLifecycleEvent` row when
  the WS socket closed. The Phase 2 P4 LWT handler in
  :mod:`fleet.presence` does the same thing now, driven by the broker's
  WILL instead of the socket close, so the consumer no longer calls it.

Both are intentionally module-level (not consumer methods) — the
``self.send_json`` / ``self.edge_id`` etc. dependencies were inlined
into explicit parameters when this was moved out of
:class:`FleetConsumer`. That keeps the legacy code importable without
having to instantiate a consumer, and removes any chance of an
accidental call path back into the live code.

This module is NOT imported from anywhere in the runtime — its only
consumer is the revert procedure documented inline above. The Phase 2
P4 commit message links to it for the same reason.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from channels.db import database_sync_to_async
from django.utils import timezone

from .models import EdgeLifecycleEvent, EdgeNode
from .protocol import (
    ERROR_BAD_FRAME,
    FRAME_HEARTBEAT,
    LIFECYCLE_SESSION_OFFLINE,
    make_ack,
)

logger = logging.getLogger(__name__)


# Status / close codes that the original consumer used — duplicated here
# so reverting does not require re-importing the consumer module.
CLOSE_BAD_FRAME_LEGACY = 4400


async def legacy_handle_heartbeat(consumer, frame: Dict[str, Any]) -> None:
    """The pre-P4 heartbeat handler — folds presence + buffer into DB.

    ``consumer`` is the :class:`FleetConsumer` instance the bound method
    used to run on; the explicit parameter keeps this function reusable
    if an operator re-binds it under a different consumer class.
    """
    if consumer.edge_id is None:
        await _fail(consumer, ERROR_BAD_FRAME, "heartbeat before register",
                    CLOSE_BAD_FRAME_LEGACY)
        return
    try:
        backlog = int(frame.get("buffer") or 0)
    except (TypeError, ValueError):
        backlog = 0
    seq = await _legacy_touch(consumer.edge_id, backlog)
    await consumer.send_json(make_ack(ref=FRAME_HEARTBEAT, last_uplink_seq=seq))


@database_sync_to_async
def _legacy_touch(pk: int, buffer_backlog: int = 0) -> int:
    """Original ``_touch`` — refresh last_seen, mirror outbox depth."""
    EdgeNode.objects.filter(pk=pk).update(
        last_seen=timezone.now(),
        status="online",
        buffer_backlog=max(0, int(buffer_backlog)),
    )
    return (
        EdgeNode.objects.filter(pk=pk)
        .values_list("last_uplink_seq", flat=True)
        .first()
    ) or 0


@database_sync_to_async
def legacy_record_session_offline(edge_pk: int) -> None:
    """Original disconnect-side ``session.offline`` synthesis.

    The Phase 2 P4 LWT handler in :mod:`fleet.presence` now does this
    via the broker's WILL retained payload, so the consumer's
    ``disconnect()`` no longer calls it. Kept here in case the broker
    is removed from the topology and the WS path becomes authoritative
    again.
    """
    try:
        EdgeLifecycleEvent.objects.create(
            edge_id=edge_pk,
            event=LIFECYCLE_SESSION_OFFLINE,
            monotonic_seq=0,
            received_at=timezone.now(),
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "fleet._legacy: failed to record session.offline edge=%s", edge_pk
        )


async def _fail(consumer, code: str, message: str, close_code: int) -> None:
    """Mirror of FleetConsumer._fail for the revert path."""
    from .protocol import make_error

    try:
        await consumer.send_json(make_error(code=code, message=message))
    except Exception:  # noqa: BLE001
        pass
    await consumer.close(code=close_code)
