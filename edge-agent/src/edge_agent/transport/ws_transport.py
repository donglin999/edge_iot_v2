"""WebSocket uplink transport (legacy M5 behaviour).

Thin adapter — every uplink frame is JSON-encoded and pushed onto the
existing per-session WebSocket. The WS connection itself is still owned
by :class:`edge_agent.agent.EdgeAgent`; this class only handles the egress
path so the agent can swap to :class:`~edge_agent.transport.mqtt_client.MqttTransport`
without forking the uplink loop.

Outbox prune semantics are unchanged from M5: the center sends
``ack {last_uplink_seq}`` back over WS, and the agent's ``_handle_ack``
prunes the outbox up to that seq. So ``confirms_on_publish`` stays
``False`` — the uplink loop must NOT prune on its own when the WS
transport is in use.
"""
from __future__ import annotations

import json
from contextlib import suppress
from typing import Any, Dict

from .base import Transport


class WsTransport(Transport):
    """Adapter that pushes uplink frames onto a live WebSocket session.

    Constructed per WS session inside ``_run_session`` — when the WS drops
    the outer reconnect loop builds a new transport for the new socket.
    The frames themselves come straight from the durable outbox; this
    class never serialises seq numbers or builds frames, only writes them.
    """

    # WS path waits for the center's explicit ack with ``last_uplink_seq``
    # before pruning. See ``EdgeAgent._handle_ack``.
    confirms_on_publish = False

    def __init__(self, ws) -> None:
        self._ws = ws

    async def connect(self) -> None:
        # Connection lifecycle stays with EdgeAgent — by the time we
        # build this adapter the WS handshake is already complete.
        return None

    async def close(self) -> None:
        if self._ws is None:
            return
        with suppress(Exception):
            await self._ws.close()

    async def _send_json(self, frame: Dict[str, Any]) -> None:
        await self._ws.send(json.dumps(frame, ensure_ascii=False))

    async def send_state(self, frame: Dict[str, Any]) -> None:
        await self._send_json(frame)

    async def send_sample(self, frame: Dict[str, Any]) -> None:
        await self._send_json(frame)

    async def send_alarm(self, frame: Dict[str, Any]) -> None:
        await self._send_json(frame)
