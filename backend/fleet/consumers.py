"""WebSocket consumer for the edge ↔ center control-plane channel.

One connection per edge-agent. Lifecycle:

1. Client opens ``ws://center/ws/fleet/`` and immediately sends a ``register``
   frame containing its edge name + activation token + agent version.
2. Center verifies the token, marks the matching `EdgeNode` as ``online``,
   stores ``version`` / ``labels``, and responds with an ``ack``.
3. Client sends ``heartbeat`` frames every ~1s. Each one updates `last_seen`
   (and re-asserts ``online`` in case a stale-sweep flipped us).
4. Unknown frames or invalid tokens produce an ``error`` frame and the
   center closes the socket.

Authentication is by activation token only; for M1 the WS is open to
anyone who can reach the center URL — TLS + a network policy belong
to a later milestone.
"""
from __future__ import annotations

import json
import logging
from enum import Enum

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.utils import timezone

from .models import EdgeNode
from .protocol import (
    ERROR_AUTH,
    ERROR_BAD_FRAME,
    ERROR_UNKNOWN_EDGE,
    FRAME_HEARTBEAT,
    FRAME_REGISTER,
    PROTOCOL_VERSION,
    make_ack,
    make_error,
)

logger = logging.getLogger(__name__)


CLOSE_AUTH = 4401
CLOSE_BAD_FRAME = 4400
CLOSE_UNKNOWN_EDGE = 4404


class AuthOutcome(Enum):
    UNKNOWN_EDGE = "unknown_edge"
    AUTH_FAILED = "auth_failed"


class FleetConsumer(AsyncJsonWebsocketConsumer):
    """Server side of the edge-agent control-plane protocol."""

    async def connect(self) -> None:
        self.edge_id: int | None = None
        self.edge_name: str | None = None
        await self.accept()
        logger.info("fleet: ws connected (awaiting register frame)")

    async def disconnect(self, code: int) -> None:
        logger.info("fleet: ws disconnected edge=%s code=%s", self.edge_name, code)

    async def receive(self, text_data=None, bytes_data=None, **kwargs):
        if text_data is None:
            await self._fail(ERROR_BAD_FRAME, "binary frame not supported", CLOSE_BAD_FRAME)
            return
        try:
            content = json.loads(text_data)
        except json.JSONDecodeError:
            await self._fail(ERROR_BAD_FRAME, "not valid JSON", CLOSE_BAD_FRAME)
            return
        await self.receive_json(content)

    async def receive_json(self, content: dict, **kwargs) -> None:
        frame_type = content.get("type") if isinstance(content, dict) else None
        if frame_type == FRAME_REGISTER:
            await self._handle_register(content)
        elif frame_type == FRAME_HEARTBEAT:
            await self._handle_heartbeat(content)
        else:
            await self._fail(ERROR_BAD_FRAME, f"unknown frame type: {frame_type!r}", CLOSE_BAD_FRAME)

    async def _handle_register(self, frame: dict) -> None:
        name = frame.get("edge_id")
        token = frame.get("token")
        version = str(frame.get("version", ""))[:64]
        labels = frame.get("labels") or {}
        if not isinstance(name, str) or not isinstance(token, str):
            await self._fail(ERROR_BAD_FRAME, "register missing edge_id/token", CLOSE_BAD_FRAME)
            return

        result = await self._lookup_and_authorize(name, token, version=version, labels=labels)
        if result is AuthOutcome.UNKNOWN_EDGE:
            await self._fail(ERROR_UNKNOWN_EDGE, f"no edge named {name!r}", CLOSE_UNKNOWN_EDGE)
            return
        if result is AuthOutcome.AUTH_FAILED:
            await self._fail(ERROR_AUTH, "invalid token", CLOSE_AUTH)
            return

        self.edge_id = result.pk
        self.edge_name = result.name
        logger.info("fleet: registered edge=%s v=%s proto=%s", result.name, version, PROTOCOL_VERSION)
        await self.send_json(make_ack(ref=FRAME_REGISTER))

    async def _handle_heartbeat(self, frame: dict) -> None:
        if self.edge_id is None:
            await self._fail(ERROR_BAD_FRAME, "heartbeat before register", CLOSE_BAD_FRAME)
            return
        await self._touch(self.edge_id)
        await self.send_json(make_ack(ref=FRAME_HEARTBEAT))

    @database_sync_to_async
    def _lookup_and_authorize(self, name: str, token: str, *, version: str, labels: dict):
        try:
            node = EdgeNode.objects.get(name=name)
        except EdgeNode.DoesNotExist:
            return AuthOutcome.UNKNOWN_EDGE
        if not node.verify_token(token):
            return AuthOutcome.AUTH_FAILED
        if labels:
            node.labels = labels
            node.save(update_fields=["labels", "updated_at"])
        node.mark_online(version=version)
        return node

    @database_sync_to_async
    def _touch(self, pk: int) -> None:
        EdgeNode.objects.filter(pk=pk).update(
            last_seen=timezone.now(),
            status="online",
        )

    async def _fail(self, code: str, message: str, close_code: int) -> None:
        try:
            await self.send_json(make_error(code=code, message=message))
        except Exception:
            pass
        await self.close(code=close_code)
