"""Center-side data-plane uplink handlers — Phase 2 P5.1 (XIU-107).

The fleet uplink protocol historically rode one transport (the per-edge
WebSocket served by :class:`fleet.consumers.FleetConsumer`). Phase 2 added
a second transport (MQTT, XIU-100/101) on top of
:data:`fleet.uplink_router.default_router`; P4 (XIU-103) bound the LWT
handler. P5.1 wires the remaining data-plane frame types — ``lifecycle``,
``sample_batch`` and ``alarm_event`` — onto the same router so an MQTT
publish lands in exactly the same DB write the WS consumer already does.

This module is the single source of truth for that DB write. Both
transports converge here:

* MQTT subscriber → :data:`default_router` → :func:`handle_lifecycle` /
  :func:`handle_sample_batch` / :func:`handle_alarm_event`.
* WS consumer (:class:`fleet.consumers.FleetConsumer`) calls the same
  ``record_*`` async wrappers after its own frame-shape validation +
  socket-close path. The recording / mirroring / fan-out side effects
  (``EdgeSample`` upsert, ``EdgeTaskStatus`` fold, InfluxDB mirror,
  :func:`fleet.consumers.broadcast_task_status`) live here and only here.

The MQTT-side ``handle_*`` coroutines do their own *log-and-drop*
validation — there is no socket to close and no ack frame to send back
(QoS 1 PUBACK from the broker is the ack). A malformed payload is
warned and dropped. The edge ``edge_name`` is resolved to the
``EdgeNode`` row before the recording call; an unknown edge is dropped
with a warning so an LWT race or a typo cannot silently auto-provision.

The ``record_*`` functions return the post-state ``last_uplink_seq``
so the WS path can still ride that on its ack frame (M5 outbox prune);
the MQTT path ignores the return value.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from channels.db import database_sync_to_async
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
    ALARM_STATES,
    FRAME_ALARM_EVENT,
    FRAME_LIFECYCLE,
    FRAME_SAMPLE_BATCH,
    LIFECYCLE_EVENTS,
    LIFECYCLE_TASK_PREFIX,
)
from .uplink_router import UplinkRouter, default_router

logger = logging.getLogger(__name__)


def _parse_ts(raw):
    """Parse an ISO-8601 string to an aware datetime, or ``None``.

    Edge-reported timestamps are informational — a malformed/missing
    value must never reject the frame, so we fall back to ``None``.
    Duplicated from :mod:`fleet.consumers` to keep this module
    self-contained (no consumer-import cycle).
    """
    if not raw:
        return None
    try:
        return parse_datetime(str(raw))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Seq classification (shared with the WS consumer)
# ---------------------------------------------------------------------------


def _classify_and_log_seq(edge: EdgeNode, seq: int, kind: str) -> str:
    """Classify an inbound uplink seq and log a gap transition."""
    verdict = classify_uplink_seq(edge.last_uplink_seq, seq)
    if verdict == UPLINK_SEQ_GAP:
        logger.warning(
            "fleet: %s uplink seq gap edge=%s last=%s got=%s "
            "(frames lost — M5 backfill)",
            kind, edge.name, edge.last_uplink_seq, seq,
        )
    return verdict


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@database_sync_to_async
def record_lifecycle(
    edge_pk: int,
    seq: int,
    event: str,
    task_id,
    task_code: str,
    error: str,
    edge_ts,
) -> int:
    """Persist a ``lifecycle`` frame: event-log row + EdgeTaskStatus fold.

    Returns the edge's post-state ``last_uplink_seq``. The WS path rides
    this on its ack frame; the MQTT path ignores it.
    """
    from configuration.models import AcqTask
    from django.db import transaction

    now = timezone.now()
    is_task_event = False
    resolved_task_id: Optional[int] = None
    with transaction.atomic():
        edge = EdgeNode.objects.select_for_update().get(pk=edge_pk)
        verdict = _classify_and_log_seq(edge, seq, "lifecycle")
        if verdict == UPLINK_SEQ_DUPLICATE:
            # Stale replay — applying it could regress the current state,
            # so drop it and leave last_uplink_seq untouched.
            logger.debug(
                "fleet: lifecycle duplicate seq=%s edge=%s — dropping",
                seq, edge.name,
            )
            return edge.last_uplink_seq

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
        # Local import to avoid a circular consumers ↔ uplink_handlers import
        # at module load. broadcast_task_status itself is sync.
        from .consumers import broadcast_task_status
        broadcast_task_status(edge_pk, resolved_task_id)
    logger.info(
        "fleet: lifecycle edge=%s event=%s seq=%s%s",
        edge_pk, event, seq, f" task={task_code}" if task_code else "",
    )
    return seq


# ---------------------------------------------------------------------------
# Sample batch
# ---------------------------------------------------------------------------


def _mirror_samples_to_influx(edge_pk, task_code, samples, window_end) -> None:
    """Best-effort short-retention InfluxDB mirror of an edge sample batch.

    Gated by ``CENTER_EDGE_SAMPLE_TO_INFLUX`` (default on). Any failure —
    InfluxDB down, not configured — is swallowed: the EdgeSample cache is
    the source of truth, the InfluxDB copy is a convenience.
    """
    if not getattr(settings, "CENTER_EDGE_SAMPLE_TO_INFLUX", True):
        return
    try:
        from .services import mirror_edge_samples

        mirror_edge_samples(edge_pk, task_code, samples, window_end)
    except Exception:  # noqa: BLE001
        logger.exception("fleet: edge sample InfluxDB mirror failed")


@database_sync_to_async
def record_sample_batch(
    edge_pk: int,
    seq: int,
    task_id: int,
    task_code: str,
    samples: list,
    window_end,
) -> int:
    """Persist a ``sample_batch`` into the EdgeSample aggregation cache.

    Returns the edge's post-state ``last_uplink_seq`` for the M5 ack.
    """
    from configuration.models import AcqTask
    from django.db import transaction

    written = 0
    with transaction.atomic():
        edge = EdgeNode.objects.select_for_update().get(pk=edge_pk)
        verdict = _classify_and_log_seq(edge, seq, "sample_batch")
        if verdict == UPLINK_SEQ_DUPLICATE:
            logger.debug(
                "fleet: sample_batch duplicate seq=%s edge=%s — dropping",
                seq, edge.name,
            )
            return edge.last_uplink_seq

        resolved_task_id = (
            task_id if AcqTask.objects.filter(pk=task_id).exists() else None
        )
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
        _mirror_samples_to_influx(edge_pk, task_code, samples, window_end)
    logger.info(
        "fleet: sample_batch edge=%s task=%s seq=%s samples=%d cached=%d",
        edge_pk, task_code or task_id, seq, len(samples), written,
    )
    return seq


# ---------------------------------------------------------------------------
# Alarm event
# ---------------------------------------------------------------------------


@database_sync_to_async
def record_alarm_event(
    edge_pk: int,
    seq: int,
    rule_id: int,
    point_code: str,
    device_code: str,
    value,
    message: str,
    status: str,
) -> int:
    """Persist an inbound ``alarm_event`` into the center alarm table.

    Reuses ``acquisition.Alarm`` with the ``edge`` FK set so the center
    ``/alarms`` page can show which edge gateway triggered the alarm.

    ``status == "firing"`` opens an idempotent alarm row; ``"cleared"``
    (M5) closes the matching open row. Returns the edge's post-state
    ``last_uplink_seq`` for the M5 ack.
    """
    from acquisition.models import Alarm, AlarmRule
    from django.db import transaction

    alarm_pk = 0
    with transaction.atomic():
        edge = EdgeNode.objects.select_for_update().get(pk=edge_pk)
        verdict = _classify_and_log_seq(edge, seq, "alarm_event")
        if verdict == UPLINK_SEQ_DUPLICATE:
            logger.debug(
                "fleet: alarm_event duplicate seq=%s edge=%s — dropping",
                seq, edge.name,
            )
            return edge.last_uplink_seq

        rule = AlarmRule.objects.filter(pk=rule_id).first()
        if rule is None:
            # Rule deleted center-side between the edge firing and us
            # receiving — drop the alarm but still advance the stream.
            logger.info(
                "fleet: alarm_event for unknown rule_id=%s edge=%s — "
                "dropped, seq still advanced",
                rule_id, edge.name,
            )
        elif status == "firing":
            # Idempotent: one open (rule, edge, point) alarm at a time.
            # A redelivered frame or an edge restart re-firing the same
            # condition must not pile up duplicate rows.
            existing = Alarm.objects.filter(
                rule=rule,
                edge=edge,
                point_code=point_code,
                device_code=device_code,
                status=Alarm.STATUS_FIRING,
            ).first()
            if existing is None:
                alarm = Alarm.objects.create(
                    rule=rule,
                    edge=edge,
                    session=None,
                    point_code=point_code,
                    device_code=device_code,
                    value=value,
                    message=message,
                    status=Alarm.STATUS_FIRING,
                )
                alarm_pk = alarm.pk
            else:
                alarm_pk = existing.pk
                logger.debug(
                    "fleet: alarm_event already open edge=%s rule=%s point=%s",
                    edge.name, rule_id, point_code,
                )
        elif status == "cleared":
            # M5 clear-transition: close the matching open alarm row.
            # Idempotent — a redelivered ``cleared`` frame, or one for an
            # alarm that never fired center-side, simply closes nothing.
            closed = (
                Alarm.objects.filter(
                    rule=rule,
                    edge=edge,
                    point_code=point_code,
                    device_code=device_code,
                    status=Alarm.STATUS_FIRING,
                ).update(
                    status=Alarm.STATUS_CLEARED,
                    value=value,
                    cleared_at=timezone.now(),
                    updated_at=timezone.now(),
                )
            )
            logger.info(
                "fleet: alarm_event cleared edge=%s rule=%s point=%s — "
                "closed %d open alarm(s)",
                edge.name, rule_id, point_code, closed,
            )

        edge.last_uplink_seq = seq
        edge.save(update_fields=["last_uplink_seq", "updated_at"])

    logger.info(
        "fleet: alarm_event edge=%s rule=%s point=%s value=%s seq=%s status=%s alarm=%s",
        edge_pk, rule_id, point_code, value, seq, status, alarm_pk or "-",
    )
    return seq


# ---------------------------------------------------------------------------
# Backfill stamp + edge resolution
# ---------------------------------------------------------------------------


@database_sync_to_async
def _stamp_backfill_at(edge_pk: int) -> None:
    EdgeNode.objects.filter(pk=edge_pk).update(
        last_backfill_at=timezone.now(), updated_at=timezone.now()
    )


@database_sync_to_async
def _resolve_edge_pk(edge_name: str) -> Optional[int]:
    return (
        EdgeNode.objects.filter(name=edge_name)
        .values_list("pk", flat=True)
        .first()
    )


async def _maybe_stamp_backfill(edge_pk: int, frame: Dict[str, Any]) -> None:
    """If the frame is tagged ``backfill: true``, stamp last_backfill_at.

    The WS path debounces this per-session via a flag on the consumer
    (so a long backfill drain only writes once). On MQTT there is no
    per-session state — every backfill-tagged frame stamps the row.
    Idempotent: ``UPDATE … SET last_backfill_at=NOW()`` is monotonic
    and cheap.
    """
    if not frame.get("backfill"):
        return
    await _stamp_backfill_at(edge_pk)


# ---------------------------------------------------------------------------
# MQTT-side router handlers
# ---------------------------------------------------------------------------


def _coerce_seq(frame: Dict[str, Any], kind: str, edge_name: str) -> Optional[int]:
    raw = frame.get("monotonic_seq")
    try:
        seq = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "fleet: %s missing/invalid monotonic_seq=%r edge=%s — dropping",
            kind, raw, edge_name,
        )
        return None
    if seq <= 0:
        logger.warning(
            "fleet: %s monotonic_seq must be >= 1 (got %s) edge=%s — dropping",
            kind, seq, edge_name,
        )
        return None
    return seq


async def _resolve_or_drop(edge_name: str, kind: str) -> Optional[int]:
    edge_pk = await _resolve_edge_pk(edge_name)
    if edge_pk is None:
        logger.warning(
            "fleet: %s for unknown edge=%r — dropping (operator must "
            "register the edge before LWT/uplink lands)",
            kind, edge_name,
        )
        return None
    return edge_pk


async def handle_lifecycle(edge_name: str, frame: Dict[str, Any]) -> None:
    """Router handler for ``lifecycle`` frames (P5.1 — XIU-107)."""
    seq = _coerce_seq(frame, "lifecycle", edge_name)
    if seq is None:
        return
    event = frame.get("event")
    if event not in LIFECYCLE_EVENTS:
        logger.warning(
            "fleet: lifecycle bad event=%r edge=%s — dropping",
            event, edge_name,
        )
        return
    task_id = None
    task_code = ""
    if event.startswith(LIFECYCLE_TASK_PREFIX):
        raw_tid = frame.get("task_id")
        try:
            task_id = int(raw_tid)
        except (TypeError, ValueError):
            logger.warning(
                "fleet: lifecycle task.* event missing/invalid task_id=%r "
                "edge=%s event=%s — dropping",
                raw_tid, edge_name, event,
            )
            return
        task_code = str(frame.get("task_code") or "")
    error = str(frame.get("error") or "")
    edge_ts = _parse_ts(frame.get("ts"))

    edge_pk = await _resolve_or_drop(edge_name, "lifecycle")
    if edge_pk is None:
        return
    await record_lifecycle(edge_pk, seq, event, task_id, task_code, error, edge_ts)
    await _maybe_stamp_backfill(edge_pk, frame)


async def handle_sample_batch(edge_name: str, frame: Dict[str, Any]) -> None:
    """Router handler for ``sample_batch`` frames (P5.1 — XIU-107)."""
    seq = _coerce_seq(frame, "sample_batch", edge_name)
    if seq is None:
        return
    raw_tid = frame.get("task_id")
    try:
        task_id = int(raw_tid)
    except (TypeError, ValueError):
        logger.warning(
            "fleet: sample_batch missing/invalid task_id=%r edge=%s — dropping",
            raw_tid, edge_name,
        )
        return
    samples = frame.get("samples")
    if not isinstance(samples, list):
        logger.warning(
            "fleet: sample_batch samples must be a list (got %s) edge=%s — dropping",
            type(samples).__name__, edge_name,
        )
        return
    task_code = str(frame.get("task_code") or "")
    window_end = _parse_ts(frame.get("window_end"))

    edge_pk = await _resolve_or_drop(edge_name, "sample_batch")
    if edge_pk is None:
        return
    await record_sample_batch(edge_pk, seq, task_id, task_code, samples, window_end)
    await _maybe_stamp_backfill(edge_pk, frame)


async def handle_alarm_event(edge_name: str, frame: Dict[str, Any]) -> None:
    """Router handler for ``alarm_event`` frames (P5.1 — XIU-107)."""
    seq = _coerce_seq(frame, "alarm_event", edge_name)
    if seq is None:
        return
    raw_rid = frame.get("rule_id")
    try:
        rule_id = int(raw_rid)
    except (TypeError, ValueError):
        logger.warning(
            "fleet: alarm_event missing/invalid rule_id=%r edge=%s — dropping",
            raw_rid, edge_name,
        )
        return
    point_code = frame.get("point_code")
    if not isinstance(point_code, str) or not point_code:
        logger.warning(
            "fleet: alarm_event missing point_code=%r edge=%s — dropping",
            point_code, edge_name,
        )
        return
    status = frame.get("status") or "firing"
    if status not in ALARM_STATES:
        logger.warning(
            "fleet: alarm_event bad status=%r edge=%s — dropping",
            status, edge_name,
        )
        return
    device_code = str(frame.get("device_code") or "")
    message = str(frame.get("message") or "")
    value = frame.get("value")

    edge_pk = await _resolve_or_drop(edge_name, "alarm_event")
    if edge_pk is None:
        return
    await record_alarm_event(
        edge_pk, seq, rule_id, point_code, device_code, value, message, status,
    )
    await _maybe_stamp_backfill(edge_pk, frame)


# ---------------------------------------------------------------------------
# Router wiring
# ---------------------------------------------------------------------------


def install(router: Optional[UplinkRouter] = None) -> None:
    """Bind the three data-plane handlers on ``router``.

    Idempotent — re-installing rebinds the same handlers. Called from
    :meth:`fleet.apps.FleetConfig.ready` so the bindings are in place
    before the MQTT subscriber thread starts consuming.
    """
    target = router or default_router
    target.set_type_handler(FRAME_LIFECYCLE, handle_lifecycle)
    target.set_type_handler(FRAME_SAMPLE_BATCH, handle_sample_batch)
    target.set_type_handler(FRAME_ALARM_EVENT, handle_alarm_event)
