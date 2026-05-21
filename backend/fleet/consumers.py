"""WebSocket consumer for the edge ↔ center control-plane channel.

One connection per edge-agent. Lifecycle:

1. Client opens ``ws://center/ws/fleet/`` and immediately sends a ``register``
   frame containing its edge name + activation token + agent version.
2. Center verifies the token, marks the matching `EdgeNode` as ``online``,
   stores ``version`` / ``labels``, joins the per-edge channel group, and
   responds with an ``ack``.
3. Client sends ``heartbeat`` frames every ~1s. Each one updates `last_seen`
   (and re-asserts ``online`` in case a stale-sweep flipped us).
4. After register the center pushes any pending ``apply_config`` snapshot
   for that edge through the per-edge group (M2+). The edge replies with
   ``config_applied`` and per-task ``task_state`` frames.
5. Unknown frames or invalid tokens produce an ``error`` frame and the
   center closes the socket.

Authentication is by activation token only; for M1/M2 the WS is open to
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

from .models import EdgeNode, EdgeTaskStatus
from .protocol import (
    ACCEPTED_PROTOCOL_VERSIONS,
    ERROR_AUTH,
    ERROR_BAD_FRAME,
    ERROR_UNKNOWN_EDGE,
    FRAME_CONFIG_APPLIED,
    FRAME_HEARTBEAT,
    FRAME_REGISTER,
    FRAME_TASK_STATE,
    PROTOCOL_VERSION,
    TASK_STATES,
    make_ack,
    make_error,
)

logger = logging.getLogger(__name__)


CLOSE_AUTH = 4401
CLOSE_BAD_FRAME = 4400
CLOSE_UNKNOWN_EDGE = 4404


def edge_group_name(edge_pk: int) -> str:
    """Channel-layer group name used for center → edge fan-out."""
    return f"fleet.edge.{edge_pk}"


class AuthOutcome(Enum):
    UNKNOWN_EDGE = "unknown_edge"
    AUTH_FAILED = "auth_failed"


class FleetConsumer(AsyncJsonWebsocketConsumer):
    """Server side of the edge-agent control-plane protocol."""

    async def connect(self) -> None:
        self.edge_id: int | None = None
        self.edge_name: str | None = None
        self._group_name: str | None = None
        await self.accept()
        logger.info("fleet: ws connected (awaiting register frame)")

    async def disconnect(self, code: int) -> None:
        if self._group_name is not None:
            try:
                await self.channel_layer.group_discard(self._group_name, self.channel_name)
            except Exception:  # noqa: BLE001
                logger.exception("fleet: group_discard failed for %s", self.edge_name)
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
        if not isinstance(content, dict):
            await self._fail(ERROR_BAD_FRAME, "frame is not an object", CLOSE_BAD_FRAME)
            return
        frame_type = content.get("type")
        if frame_type == FRAME_REGISTER:
            await self._handle_register(content)
        elif frame_type == FRAME_HEARTBEAT:
            await self._handle_heartbeat(content)
        elif frame_type == FRAME_CONFIG_APPLIED:
            await self._handle_config_applied(content)
        elif frame_type == FRAME_TASK_STATE:
            await self._handle_task_state(content)
        else:
            await self._fail(ERROR_BAD_FRAME, f"unknown frame type: {frame_type!r}", CLOSE_BAD_FRAME)

    async def _handle_register(self, frame: dict) -> None:
        name = frame.get("edge_id")
        token = frame.get("token")
        version = str(frame.get("version", ""))[:64]
        labels = frame.get("labels") or {}
        wire_version = str(frame.get("v", ""))
        if not isinstance(name, str) or not isinstance(token, str):
            await self._fail(ERROR_BAD_FRAME, "register missing edge_id/token", CLOSE_BAD_FRAME)
            return
        if wire_version and wire_version not in ACCEPTED_PROTOCOL_VERSIONS:
            await self._fail(
                ERROR_BAD_FRAME,
                f"unsupported protocol version: {wire_version!r}",
                CLOSE_BAD_FRAME,
            )
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
        self._group_name = edge_group_name(result.pk)
        await self.channel_layer.group_add(self._group_name, self.channel_name)
        logger.info(
            "fleet: registered edge=%s v=%s wire=%s proto=%s",
            result.name, version, wire_version or "n/a", PROTOCOL_VERSION,
        )
        await self.send_json(make_ack(ref=FRAME_REGISTER))

    async def _handle_heartbeat(self, frame: dict) -> None:
        if self.edge_id is None:
            await self._fail(ERROR_BAD_FRAME, "heartbeat before register", CLOSE_BAD_FRAME)
            return
        await self._touch(self.edge_id)
        await self.send_json(make_ack(ref=FRAME_HEARTBEAT))

    async def _handle_config_applied(self, frame: dict) -> None:
        if self.edge_id is None:
            await self._fail(ERROR_BAD_FRAME, "config_applied before register", CLOSE_BAD_FRAME)
            return
        try:
            version = int(frame.get("version"))
        except (TypeError, ValueError):
            await self._fail(ERROR_BAD_FRAME, "config_applied missing/invalid version", CLOSE_BAD_FRAME)
            return
        status = frame.get("status")
        if status not in ("ok", "error"):
            await self._fail(ERROR_BAD_FRAME, "config_applied bad status", CLOSE_BAD_FRAME)
            return
        error = frame.get("error") or ""
        await self._record_config_applied(self.edge_id, version, status, error)
        await self.send_json(make_ack(ref=FRAME_CONFIG_APPLIED))

    async def _handle_task_state(self, frame: dict) -> None:
        if self.edge_id is None:
            await self._fail(ERROR_BAD_FRAME, "task_state before register", CLOSE_BAD_FRAME)
            return
        try:
            task_id = int(frame.get("task_id"))
        except (TypeError, ValueError):
            await self._fail(ERROR_BAD_FRAME, "task_state missing/invalid task_id", CLOSE_BAD_FRAME)
            return
        state = frame.get("state")
        if state not in TASK_STATES:
            await self._fail(ERROR_BAD_FRAME, f"task_state bad state: {state!r}", CLOSE_BAD_FRAME)
            return
        error = frame.get("error") or ""
        await self._record_task_state(self.edge_id, task_id, state, error)
        await self.send_json(make_ack(ref=FRAME_TASK_STATE))

    # ---- channel-layer fan-out -------------------------------------------

    async def fleet_send(self, event: dict) -> None:
        """Channel-layer hook: forward a pre-built frame to this socket.

        Producers (e.g. assignments_sync view) publish to the per-edge
        group with ``type="fleet.send"`` and ``frame=<json-serializable>``.
        """
        frame = event.get("frame")
        if not isinstance(frame, dict):
            logger.warning("fleet_send: dropping non-dict frame: %r", frame)
            return
        await self.send_json(frame)

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

    @database_sync_to_async
    def _record_config_applied(self, edge_pk: int, version: int, status: str, error: str) -> None:
        from .models import EdgeAssignment  # local to avoid early-import cycles

        now = timezone.now()
        # Stamp the per-edge assignments with whichever version the edge
        # confirmed it ran. We only advance ``last_applied_version`` on a
        # successful apply; an error keeps the prior value so the UI can
        # show "edge stuck at vN".
        if status == "ok":
            EdgeAssignment.objects.filter(edge_id=edge_pk).update(
                last_applied_version=version,
                applied_at=now,
                updated_at=now,
            )
        logger.info(
            "fleet: config_applied edge=%s v=%s status=%s%s",
            edge_pk, version, status, f" err={error}" if error else "",
        )

    @database_sync_to_async
    def _record_task_state(self, edge_pk: int, task_id: int, state: str, error: str) -> None:
        # If the task no longer exists (deleted center-side between the
        # edge sending the frame and us receiving it) the row insert will
        # IntegrityError; we tolerate that — drop the report silently.
        from configuration.models import AcqTask

        if not AcqTask.objects.filter(pk=task_id).exists():
            logger.info(
                "fleet: task_state for unknown task_id=%s on edge=%s — dropping",
                task_id, edge_pk,
            )
            return
        EdgeTaskStatus.objects.update_or_create(
            edge_id=edge_pk,
            task_id=task_id,
            defaults={
                "state": state,
                "error": error,
                "last_reported_at": timezone.now(),
            },
        )

    async def _fail(self, code: str, message: str, close_code: int) -> None:
        try:
            await self.send_json(make_error(code=code, message=message))
        except Exception:
            pass
        await self.close(code=close_code)
