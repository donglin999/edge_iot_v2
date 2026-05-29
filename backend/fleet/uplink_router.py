"""Single dispatch surface for inbound edge-uplink frames (XIU-100 Phase 2).

The fleet protocol historically arrives over one transport — a per-edge
WebSocket served by :class:`fleet.consumers.FleetConsumer`. Phase 2 of the
migration adds a second transport (MQTT, XIU-100) and the down-stream
delivery is unchanged regardless of which transport carried the bytes.

To keep the two transports honest, both call into a single
:class:`UplinkRouter` instance with a parsed JSON frame and the edge name
(extracted by the transport from the topic path or the ``register``
frame). For Phase 2 P1 the router was intentionally a thin pass-through —
it logged the frame and handed it to whichever handler the transport
bound. Phase 2 P4 (XIU-103) wires the first real handler: ``lwt`` frames
fold straight into :class:`fleet.models.EdgeNode.status`, replacing the
WS-heartbeat-driven presence detection that the consumer used to own.
Wiring the rest of FleetConsumer's per-frame handlers through here is
still later-Phase-2 work.

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


# Phase 2 P4 (XIU-103) — LWT frame type. The MQTT subscriber stamps
# ``frame["type"] = "lwt"`` on every payload it reads off ``edge/+/lwt``,
# regardless of what shape the edge published. Handlers route on that.
LWT_FRAME_TYPE = "lwt"


class UplinkRouter:
    """Route an inbound uplink frame to the bound handler.

    A transport produces ``(edge_name, frame)`` and calls
    :meth:`dispatch`. If a handler has been registered via
    :meth:`set_handler`, the frame is forwarded; otherwise the router
    only logs (the Phase 2 P1 default — see module docstring).

    Phase 2 P4 introduces a *per-frame-type* handler table so the LWT
    presence handler can be wired without taking over the rest of the
    routing surface. ``set_handler`` (the fallback) still wins when no
    type-specific handler is registered, so an integrator opting in to
    "everything through one funnel" keeps working.
    """

    def __init__(self) -> None:
        self._handler: Optional[FrameHandler] = None
        self._type_handlers: Dict[str, FrameHandler] = {}

    def set_handler(self, handler: Optional[FrameHandler]) -> None:
        """Bind (or unbind) the fallback handler for any frame type.

        Phase 2 later milestones will register FleetConsumer's frame
        dispatcher here so both transports converge on the same code.
        """
        self._handler = handler

    def set_type_handler(
        self, frame_type: str, handler: Optional[FrameHandler]
    ) -> None:
        """Bind a handler for *one* frame type (P4 — XIU-103).

        ``handler=None`` clears the binding. A type-specific handler is
        preferred over the fallback when both are set, so the LWT path
        can land on a focused coroutine without the integrator having
        to consolidate every frame type behind one switch statement.
        """
        if handler is None:
            self._type_handlers.pop(frame_type, None)
        else:
            self._type_handlers[frame_type] = handler

    async def dispatch(self, edge_name: str, frame: Dict[str, Any]) -> None:
        """Hand a frame to the bound handler (or log + drop)."""
        frame_type = frame.get("type", "?")
        handler = self._type_handlers.get(frame_type) or self._handler
        if handler is None:
            logger.info(
                "uplink_router: no handler bound — dropping edge=%s type=%s",
                edge_name, frame_type,
            )
            return
        try:
            await handler(edge_name, frame)
        except Exception:  # noqa: BLE001
            logger.exception(
                "uplink_router: handler raised on edge=%s type=%s",
                edge_name, frame_type,
            )


# Process-wide singleton. Each transport imports this and either calls
# ``dispatch`` (subscriber side) or ``set_handler`` (wire-up at app ready).
default_router = UplinkRouter()
