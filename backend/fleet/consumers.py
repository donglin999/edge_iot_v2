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

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from channels.layers import get_channel_layer
from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import (
    EdgeLifecycleEvent,
    EdgeNode,
    EdgeSample,
    EdgeTaskStatus,
    UPLINK_SEQ_DUPLICATE,
    UPLINK_SEQ_GAP,
    classify_uplink_seq,
)
from .protocol import (
    ACCEPTED_PROTOCOL_VERSIONS,
    ERROR_AUTH,
    ERROR_BAD_FRAME,
    ERROR_UNKNOWN_EDGE,
    FRAME_CONFIG_APPLIED,
    FRAME_HEARTBEAT,
    FRAME_LIFECYCLE,
    FRAME_REGISTER,
    FRAME_SAMPLE_BATCH,
    FRAME_TASK_STATE,
    LIFECYCLE_EVENTS,
    LIFECYCLE_SESSION_OFFLINE,
    LIFECYCLE_TASK_PREFIX,
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


def _parse_ts(raw):
    """Parse an ISO-8601 string into an aware datetime, or ``None``.

    Edge-reported timestamps are informational — a malformed or missing
    value must never reject the frame, so we just fall back to ``None``.
    """
    if not raw:
        return None
    try:
        return parse_datetime(str(raw))
    except (ValueError, TypeError):
        return None


# Channel-layer group browser /acquisition clients subscribe to for live
# edge task-status updates (M3 — replaces the M2 REST polling).
TASK_STATUS_GROUP = "fleet.task_status"


def _serialize_task_status(status: "EdgeTaskStatus") -> dict:
    """Shape an EdgeTaskStatus row for the browser task-status WS feed.

    Mirrors ``EdgeTaskStatusSerializer`` so the frontend can consume the
    REST snapshot and the WS pushes with one code path.
    """
    return {
        "id": status.id,
        "edge": status.edge_id,
        "edge_name": status.edge.name,
        "task": status.task_id,
        "task_code": status.task.code,
        "state": status.state,
        "error": status.error,
        "last_reported_at": (
            status.last_reported_at.isoformat() if status.last_reported_at else None
        ),
        "updated_at": status.updated_at.isoformat() if status.updated_at else None,
    }


def broadcast_task_status(edge_pk: int, task_id: int) -> None:
    """Push the latest EdgeTaskStatus row to subscribed browser WS clients.

    Best-effort: a missing channel layer or a vanished row is a no-op. Run
    from a sync (``database_sync_to_async``) context — uses ``async_to_sync``
    for the cross-thread ``group_send`` exactly like the acquisition sinks.
    """
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        status = EdgeTaskStatus.objects.select_related("edge", "task").get(
            edge_id=edge_pk, task_id=task_id
        )
    except EdgeTaskStatus.DoesNotExist:
        return
    try:
        async_to_sync(layer.group_send)(
            TASK_STATUS_GROUP,
            {"type": "fleet.task_status", "data": _serialize_task_status(status)},
        )
    except Exception:  # noqa: BLE001
        logger.exception("fleet: task_status broadcast failed")


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
        # M3: a closing socket cannot send a final lifecycle frame, so the
        # center synthesises the ``session.offline`` event itself.
        if self.edge_id is not None:
            try:
                await self._record_session_offline(self.edge_id)
            except Exception:  # noqa: BLE001
                logger.exception("fleet: session.offline record failed for %s", self.edge_name)
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
        elif frame_type == FRAME_LIFECYCLE:
            await self._handle_lifecycle(content)
        elif frame_type == FRAME_SAMPLE_BATCH:
            await self._handle_sample_batch(content)
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

    async def _handle_lifecycle(self, frame: dict) -> None:
        """v0.3: a session/task lifecycle event carrying ``monotonic_seq``."""
        if self.edge_id is None:
            await self._fail(ERROR_BAD_FRAME, "lifecycle before register", CLOSE_BAD_FRAME)
            return
        try:
            seq = int(frame.get("monotonic_seq"))
        except (TypeError, ValueError):
            await self._fail(ERROR_BAD_FRAME, "lifecycle missing/invalid monotonic_seq", CLOSE_BAD_FRAME)
            return
        if seq <= 0:
            await self._fail(ERROR_BAD_FRAME, "lifecycle monotonic_seq must be >= 1", CLOSE_BAD_FRAME)
            return
        event = frame.get("event")
        if event not in LIFECYCLE_EVENTS:
            await self._fail(ERROR_BAD_FRAME, f"lifecycle bad event: {event!r}", CLOSE_BAD_FRAME)
            return
        task_id = None
        task_code = ""
        if event.startswith(LIFECYCLE_TASK_PREFIX):
            try:
                task_id = int(frame.get("task_id"))
            except (TypeError, ValueError):
                await self._fail(ERROR_BAD_FRAME, "lifecycle task event missing task_id", CLOSE_BAD_FRAME)
                return
            task_code = str(frame.get("task_code") or "")
        error = frame.get("error") or ""
        edge_ts = _parse_ts(frame.get("ts"))
        await self._record_lifecycle(self.edge_id, seq, event, task_id, task_code, error, edge_ts)
        await self.send_json(make_ack(ref=FRAME_LIFECYCLE))

    async def _handle_sample_batch(self, frame: dict) -> None:
        """v0.3: one aggregation window of point samples carrying a seq."""
        if self.edge_id is None:
            await self._fail(ERROR_BAD_FRAME, "sample_batch before register", CLOSE_BAD_FRAME)
            return
        try:
            seq = int(frame.get("monotonic_seq"))
        except (TypeError, ValueError):
            await self._fail(ERROR_BAD_FRAME, "sample_batch missing/invalid monotonic_seq", CLOSE_BAD_FRAME)
            return
        if seq <= 0:
            await self._fail(ERROR_BAD_FRAME, "sample_batch monotonic_seq must be >= 1", CLOSE_BAD_FRAME)
            return
        try:
            task_id = int(frame.get("task_id"))
        except (TypeError, ValueError):
            await self._fail(ERROR_BAD_FRAME, "sample_batch missing/invalid task_id", CLOSE_BAD_FRAME)
            return
        samples = frame.get("samples")
        if not isinstance(samples, list):
            await self._fail(ERROR_BAD_FRAME, "sample_batch samples must be a list", CLOSE_BAD_FRAME)
            return
        task_code = str(frame.get("task_code") or "")
        window_end = _parse_ts(frame.get("window_end"))
        await self._record_sample_batch(self.edge_id, seq, task_id, task_code, samples, window_end)
        await self.send_json(make_ack(ref=FRAME_SAMPLE_BATCH))

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
        # Push the change to browser /acquisition clients too — a v0.2 edge
        # (rolling-upgrade window) reports via task_state, not lifecycle, so
        # without this its status changes would never reach the live feed.
        broadcast_task_status(edge_pk, task_id)

    # ---- M3 uplink: lifecycle + sample_batch ------------------------------

    def _classify_and_log_seq(self, edge: EdgeNode, seq: int, kind: str) -> str:
        """Classify an inbound uplink seq and log a gap transition."""
        verdict = classify_uplink_seq(edge.last_uplink_seq, seq)
        if verdict == UPLINK_SEQ_GAP:
            logger.warning(
                "fleet: %s uplink seq gap edge=%s last=%s got=%s "
                "(frames lost — M5 backfill)",
                kind, edge.name, edge.last_uplink_seq, seq,
            )
        return verdict

    @database_sync_to_async
    def _record_lifecycle(
        self,
        edge_pk: int,
        seq: int,
        event: str,
        task_id,
        task_code: str,
        error: str,
        edge_ts,
    ) -> None:
        """Persist a ``lifecycle`` frame: event-log row + EdgeTaskStatus fold."""
        from configuration.models import AcqTask
        from django.db import transaction

        now = timezone.now()
        with transaction.atomic():
            edge = EdgeNode.objects.select_for_update().get(pk=edge_pk)
            verdict = self._classify_and_log_seq(edge, seq, "lifecycle")
            if verdict == UPLINK_SEQ_DUPLICATE:
                # Stale replay — applying it could regress the current
                # state, so drop it and leave last_uplink_seq untouched.
                logger.debug(
                    "fleet: lifecycle duplicate seq=%s edge=%s — dropping",
                    seq, edge.name,
                )
                return

            resolved_task_id = None
            if task_id is not None and AcqTask.objects.filter(pk=task_id).exists():
                resolved_task_id = task_id

            EdgeLifecycleEvent.objects.create(
                edge=edge,
                task_id=resolved_task_id,
                event=event,
                monotonic_seq=seq,
                error=error,
                edge_ts=edge_ts,
                received_at=now,
            )
            edge.last_uplink_seq = seq
            edge.save(update_fields=["last_uplink_seq", "updated_at"])

            # task.* events also fold into the current-state EdgeTaskStatus
            # row so the existing /acquisition projection keeps working.
            is_task_event = (
                event.startswith(LIFECYCLE_TASK_PREFIX) and resolved_task_id is not None
            )
            if is_task_event:
                state = event[len(LIFECYCLE_TASK_PREFIX):]
                EdgeTaskStatus.objects.update_or_create(
                    edge_id=edge_pk,
                    task_id=resolved_task_id,
                    defaults={
                        "state": state,
                        "error": error,
                        "last_reported_at": now,
                    },
                )
        # After commit: push the new status to browser /acquisition clients
        # so the operator's edge badge changes in real time (no polling).
        if is_task_event:
            broadcast_task_status(edge_pk, resolved_task_id)
        logger.info(
            "fleet: lifecycle edge=%s event=%s seq=%s%s",
            edge_pk, event, seq, f" task={task_code}" if task_code else "",
        )

    @database_sync_to_async
    def _record_sample_batch(
        self,
        edge_pk: int,
        seq: int,
        task_id: int,
        task_code: str,
        samples: list,
        window_end,
    ) -> int:
        """Persist a ``sample_batch`` into the EdgeSample aggregation cache.

        Returns the number of samples folded into the cache (0 on a dropped
        duplicate or an unknown task).
        """
        from configuration.models import AcqTask
        from django.db import transaction

        with transaction.atomic():
            edge = EdgeNode.objects.select_for_update().get(pk=edge_pk)
            verdict = self._classify_and_log_seq(edge, seq, "sample_batch")
            if verdict == UPLINK_SEQ_DUPLICATE:
                logger.debug(
                    "fleet: sample_batch duplicate seq=%s edge=%s — dropping",
                    seq, edge.name,
                )
                return 0

            resolved_task_id = (
                task_id if AcqTask.objects.filter(pk=task_id).exists() else None
            )
            written = 0
            if resolved_task_id is None:
                logger.info(
                    "fleet: sample_batch for unknown task_id=%s edge=%s — "
                    "samples dropped, seq still advanced",
                    task_id, edge.name,
                )
            else:
                for sample in samples:
                    if not isinstance(sample, dict):
                        continue
                    code = sample.get("point_code")
                    if not code:
                        continue
                    EdgeSample.objects.update_or_create(
                        edge=edge,
                        task_id=resolved_task_id,
                        point_code=str(code)[:128],
                        defaults={
                            "value": sample.get("value"),
                            "quality": str(sample.get("quality") or "good")[:16],
                            "sample_ts": _parse_ts(sample.get("timestamp")),
                            "monotonic_seq": seq,
                            "window_end": window_end,
                        },
                    )
                    written += 1

            # The frame was processed regardless of whether the samples
            # landed — advance the stream position so seq stays monotonic.
            edge.last_uplink_seq = seq
            edge.save(update_fields=["last_uplink_seq", "updated_at"])

        if written:
            self._mirror_samples_to_influx(edge_pk, task_code, samples, window_end)
        logger.info(
            "fleet: sample_batch edge=%s task=%s seq=%s samples=%d cached=%d",
            edge_pk, task_code or task_id, seq, len(samples), written,
        )
        return written

    @staticmethod
    def _mirror_samples_to_influx(edge_pk, task_code, samples, window_end) -> None:
        """Best-effort short-retention InfluxDB mirror of an edge sample batch.

        Gated by ``CENTER_EDGE_SAMPLE_TO_INFLUX`` (default on). Any failure
        — InfluxDB down, not configured — is swallowed: the EdgeSample
        cache is the source of truth, the InfluxDB copy is a convenience.
        """
        if not getattr(settings, "CENTER_EDGE_SAMPLE_TO_INFLUX", True):
            return
        try:
            from .services import mirror_edge_samples

            mirror_edge_samples(edge_pk, task_code, samples, window_end)
        except Exception:  # noqa: BLE001
            logger.exception("fleet: edge sample InfluxDB mirror failed")

    @database_sync_to_async
    def _record_session_offline(self, edge_pk: int) -> None:
        """Synthesise a ``session.offline`` lifecycle row on WS disconnect."""
        try:
            EdgeLifecycleEvent.objects.create(
                edge_id=edge_pk,
                event=LIFECYCLE_SESSION_OFFLINE,
                monotonic_seq=0,
                received_at=timezone.now(),
            )
        except Exception:  # noqa: BLE001
            logger.exception("fleet: failed to record session.offline edge=%s", edge_pk)

    async def _fail(self, code: str, message: str, close_code: int) -> None:
        try:
            await self.send_json(make_error(code=code, message=message))
        except Exception:
            pass
        await self.close(code=close_code)


class FleetTaskStatusConsumer(AsyncJsonWebsocketConsumer):
    """Browser-facing WS feed of live edge task statuses (M3).

    Endpoint: ``ws://<center>/ws/fleet/task-statuses/``. On connect the
    client gets a full snapshot; thereafter one ``task_status`` message per
    change, pushed from :func:`broadcast_task_status` when an inbound
    ``lifecycle`` frame folds a new state into ``EdgeTaskStatus``. This
    replaces the M2 REST polling of ``/api/fleet/task-statuses/`` on the
    operator's ``/acquisition`` page — no auth (same trust model as the
    rest of the fleet control plane this milestone).
    """

    async def connect(self) -> None:
        await self.channel_layer.group_add(TASK_STATUS_GROUP, self.channel_name)
        await self.accept()
        try:
            snapshot = await self._snapshot()
        except Exception:  # noqa: BLE001
            logger.exception("fleet: task-status snapshot failed")
            snapshot = []
        await self.send_json({"type": "snapshot", "data": snapshot})

    async def disconnect(self, code: int) -> None:
        try:
            await self.channel_layer.group_discard(TASK_STATUS_GROUP, self.channel_name)
        except Exception:  # noqa: BLE001
            logger.exception("fleet: task-status group_discard failed")

    async def receive_json(self, content: dict, **kwargs) -> None:
        # Read-only feed — clients never send anything meaningful.
        return None

    async def fleet_task_status(self, event: dict) -> None:
        """Channel-layer hook: forward one task-status change to the client."""
        await self.send_json({"type": "task_status", "data": event.get("data")})

    @database_sync_to_async
    def _snapshot(self) -> list:
        qs = EdgeTaskStatus.objects.select_related("edge", "task").order_by("edge", "task")
        return [_serialize_task_status(s) for s in qs]
