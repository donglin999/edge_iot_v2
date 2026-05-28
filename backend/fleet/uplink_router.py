"""Single dispatch surface for inbound edge-uplink frames (XIU-100 Phase 2).

The fleet protocol historically arrives over one transport — a per-edge
WebSocket served by :class:`fleet.consumers.FleetConsumer`. Phase 2 of the
migration adds a second transport (MQTT, XIU-100) and the down-stream
delivery is unchanged regardless of which transport carried the bytes.

To keep the two transports honest, both call into a single
:class:`UplinkRouter` instance with a parsed JSON frame and the edge name
(extracted by the transport from the topic path or the ``register``
frame). For Phase 2 P1 the router is intentionally a thin pass-through —
it logs the frame and hands it to whichever handler the transport binds.
Wiring FleetConsumer's ``_handle_*`` methods through here is a later
milestone; today's MQTT subscriber simply logs because the canonical edge
↔ DB plumbing still lives inside FleetConsumer's ``receive_json`` path.

Thread/async model: ``dispatch`` is an async coroutine; all transports
running under the daphne ASGI loop call it from inside the same loop, so
no extra locking is required. Handlers can ``await`` DB work through
``channels.db.database_sync_to_async`` the same way the WS consumer does.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)


FrameHandler = Callable[[str, Dict[str, Any]], Awaitable[None]]


class UplinkRouter:
    """Route an inbound uplink frame to the bound handler.

    A transport produces ``(edge_name, frame)`` and calls
    :meth:`dispatch`. If a handler has been registered via
    :meth:`set_handler`, the frame is forwarded; otherwise the router
    only logs (the Phase 2 P1 default — see module docstring).
    """

    def __init__(self) -> None:
        self._handler: Optional[FrameHandler] = None

    def set_handler(self, handler: Optional[FrameHandler]) -> None:
        """Bind (or unbind) the downstream handler.

        Phase 2 later milestones will register FleetConsumer's frame
        dispatcher here so both transports converge on the same code.
        """
        self._handler = handler

    async def dispatch(self, edge_name: str, frame: Dict[str, Any]) -> None:
        """Hand a frame to the bound handler (or log + drop)."""
        frame_type = frame.get("type", "?")
        if self._handler is None:
            logger.info(
                "uplink_router: no handler bound — dropping edge=%s type=%s",
                edge_name, frame_type,
            )
            return
        try:
            await self._handler(edge_name, frame)
        except Exception:  # noqa: BLE001
            logger.exception(
                "uplink_router: handler raised on edge=%s type=%s",
                edge_name, frame_type,
            )


# Process-wide singleton. Each transport imports this and either calls
# ``dispatch`` (subscriber side) or ``set_handler`` (wire-up at app ready).
default_router = UplinkRouter()
