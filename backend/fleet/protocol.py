"""Distributed control-plane wire protocol — frame helpers.

Single source of truth for the JSON frames exchanged between the center
and each `edge-agent` over `/ws/fleet/`. The protocol doc lives in
``docs/distributed/protocol.md`` and is authoritative.

v0.4 (M4) adds the alarm channel — a pure increment over v0.3:

- ``apply_config`` now also carries an ``alarm_rules`` array (threshold
  rules the edge evaluates locally). The field is optional; a v0.3 edge
  ignores it and a v0.4 center sending to a v0.3 edge is harmless.
- ``alarm_event`` edge → center  a locally-triggered alarm + ``monotonic_seq``

v0.3 (M3) adds the uplink data channel:

- ``lifecycle``    edge → center  session/task lifecycle event + ``monotonic_seq``
- ``sample_batch`` edge → center  1 Hz aggregated samples + ``monotonic_seq``

v0.2 (M2) added the config-push channel:

- ``apply_config``   center → edge  full task/device/point snapshot
- ``config_applied`` edge   → center result of applying a snapshot
- ``task_state``     edge   → center per-task lifecycle state (retained for
                                    v0.2 edges; v0.3 edges emit ``lifecycle``)

Older v0.1 frames (``register``/``heartbeat``/``ack``/``error``) carry
unchanged semantics; the center accepts ``v=0.1``/``0.2``/``0.3`` on
register so a stale agent can still connect during a rolling upgrade.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List

PROTOCOL_VERSION = "0.5"
# Versions the center is willing to talk to. Add older majors here when
# an upgrade boundary needs explicit shim support.
ACCEPTED_PROTOCOL_VERSIONS = frozenset({"0.1", "0.2", "0.3", "0.4", "0.5"})

# --- frame type constants ---------------------------------------------------

# v0.1
FRAME_REGISTER = "register"
FRAME_HEARTBEAT = "heartbeat"
FRAME_ERROR = "error"
FRAME_ACK = "ack"
# v0.2
FRAME_APPLY_CONFIG = "apply_config"
FRAME_CONFIG_APPLIED = "config_applied"
FRAME_TASK_STATE = "task_state"
# v0.3
FRAME_LIFECYCLE = "lifecycle"
FRAME_SAMPLE_BATCH = "sample_batch"
# v0.4
FRAME_ALARM_EVENT = "alarm_event"

# --- error codes ------------------------------------------------------------

ERROR_AUTH = "auth_failed"
ERROR_BAD_FRAME = "bad_frame"
ERROR_UNKNOWN_EDGE = "unknown_edge"

# --- task lifecycle states (edge → center) ---------------------------------

TASK_STATE_STARTING = "starting"
TASK_STATE_RUNNING = "running"
TASK_STATE_STOPPING = "stopping"
TASK_STATE_STOPPED = "stopped"
TASK_STATE_ERROR = "error"

TASK_STATES: Iterable[str] = (
    TASK_STATE_STARTING,
    TASK_STATE_RUNNING,
    TASK_STATE_STOPPING,
    TASK_STATE_STOPPED,
    TASK_STATE_ERROR,
)

CONFIG_APPLIED_OK = "ok"
CONFIG_APPLIED_ERROR = "error"

# --- v0.3 lifecycle events --------------------------------------------------

# Session-scoped events carry no task_id/task_code.
LIFECYCLE_SESSION_ONLINE = "session.online"
# Synthesised center-side on WS disconnect — never sent by the edge.
LIFECYCLE_SESSION_OFFLINE = "session.offline"

# Task-scoped events. The suffix after "task." equals the v0.2 task_state
# string so the center can fold both into one ``EdgeTaskStatus.state``.
LIFECYCLE_TASK_PREFIX = "task."
LIFECYCLE_TASK_EVENTS: Dict[str, str] = {
    TASK_STATE_STARTING: LIFECYCLE_TASK_PREFIX + TASK_STATE_STARTING,
    TASK_STATE_RUNNING: LIFECYCLE_TASK_PREFIX + TASK_STATE_RUNNING,
    TASK_STATE_STOPPING: LIFECYCLE_TASK_PREFIX + TASK_STATE_STOPPING,
    TASK_STATE_STOPPED: LIFECYCLE_TASK_PREFIX + TASK_STATE_STOPPED,
    TASK_STATE_ERROR: LIFECYCLE_TASK_PREFIX + TASK_STATE_ERROR,
}

# Every event name the wire protocol allows on an inbound ``lifecycle``
# frame. ``session.offline`` is excluded — it is center-synthesised only.
LIFECYCLE_EVENTS: frozenset = frozenset(
    {LIFECYCLE_SESSION_ONLINE} | set(LIFECYCLE_TASK_EVENTS.values())
)

SAMPLE_QUALITIES: frozenset = frozenset({"good", "bad", "uncertain"})

# --- v0.4 alarm event states ------------------------------------------------

# Reported on an inbound ``alarm_event`` frame. M4 emits ``firing`` only;
# ``cleared`` is reserved (clear-transition uplink is M5).
ALARM_STATE_FIRING = "firing"
ALARM_STATE_CLEARED = "cleared"
ALARM_STATES: frozenset = frozenset({ALARM_STATE_FIRING, ALARM_STATE_CLEARED})


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def make_register(
    *,
    edge_id: str,
    token: str,
    version: str,
    labels: Dict[str, Any] | None = None,
    uplink_seq: int = 0,
) -> Dict[str, Any]:
    """Edge → center: first frame of a session.

    ``uplink_seq`` (v0.5) is the edge's persistent uplink high-water mark —
    the center compares it against its own ``last_uplink_seq`` to detect an
    edge whose durable buffer was wiped. Additive; a v0.4 center ignores it.
    """
    return {
        "v": PROTOCOL_VERSION,
        "type": FRAME_REGISTER,
        "edge_id": edge_id,
        "token": token,
        "version": version,
        "labels": labels or {},
        "uplink_seq": int(uplink_seq),
    }


def make_heartbeat(
    *,
    edge_id: str,
    cpu: float = 0.0,
    mem: float = 0.0,
    uptime: float = 0.0,
    tasks: int = 0,
    buffer: int = 0,
) -> Dict[str, Any]:
    """Edge → center: ~1 Hz liveness ping.

    ``buffer`` (v0.5) is the depth of the edge's durable uplink outbox; the
    center mirrors it onto ``EdgeNode.buffer_backlog``. Additive.
    """
    return {
        "v": PROTOCOL_VERSION,
        "type": FRAME_HEARTBEAT,
        "edge_id": edge_id,
        "cpu": cpu,
        "mem": mem,
        "uptime": uptime,
        "tasks": tasks,
        "buffer": int(buffer),
    }


def make_ack(*, ref: str | None = None, last_uplink_seq: int | None = None) -> Dict[str, Any]:
    """Center → edge: acknowledge a prior frame.

    ``last_uplink_seq`` (M5) carries the center's current high-water uplink
    sequence for this edge. It rides every ack — ``register`` (so the edge
    knows where to start its reconnect backfill), ``heartbeat`` and each
    per-uplink-frame ack (so the edge can prune its durable outbox up to
    the confirmed seq). Additive: a pre-M5 edge simply ignores the field.
    """
    frame: Dict[str, Any] = {"v": PROTOCOL_VERSION, "type": FRAME_ACK, "ref": ref}
    if last_uplink_seq is not None:
        frame["last_uplink_seq"] = int(last_uplink_seq)
    return frame


def make_error(*, code: str, message: str = "") -> Dict[str, Any]:
    return {
        "v": PROTOCOL_VERSION,
        "type": FRAME_ERROR,
        "code": code,
        "message": message,
    }


def make_apply_config(
    *,
    version: int,
    tasks: list[Dict[str, Any]],
    devices: list[Dict[str, Any]],
    points: list[Dict[str, Any]],
    alarm_rules: list[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Center → edge: full configuration snapshot for the edge to apply.

    ``version`` is a monotonically-increasing integer per edge — the edge
    persists it as ``last_applied_version`` so a redelivered or stale frame
    can be rejected by comparison.

    ``alarm_rules`` (v0.4) is the set of threshold rules the edge evaluates
    locally; it is always present from a v0.4 center (possibly empty) and
    silently ignored by a v0.3 edge.
    """
    return {
        "v": PROTOCOL_VERSION,
        "type": FRAME_APPLY_CONFIG,
        "version": int(version),
        "tasks": list(tasks),
        "devices": list(devices),
        "points": list(points),
        "alarm_rules": list(alarm_rules or []),
    }


def make_alarm_event(
    *,
    edge_id: str,
    monotonic_seq: int,
    rule_id: int,
    point_code: str,
    value: Any,
    device_code: str = "",
    severity: str = "warning",
    status: str = ALARM_STATE_FIRING,
    message: str = "",
    fired_at: str | None = None,
) -> Dict[str, Any]:
    """Edge → center: a locally-triggered alarm (v0.4).

    The edge evaluates the threshold rules pushed via ``apply_config``
    against its acquisition readings and emits one ``alarm_event`` per
    fire transition. ``rule_id`` is the center-side ``AlarmRule.pk`` (rule
    definitions live at the center; the edge only mirrors them) so the
    center can re-attach the event to its own rule row.
    """
    if status not in ALARM_STATES:
        raise ValueError(f"invalid alarm_event status: {status!r}")
    return {
        "v": PROTOCOL_VERSION,
        "type": FRAME_ALARM_EVENT,
        "edge_id": edge_id,
        "monotonic_seq": int(monotonic_seq),
        "rule_id": int(rule_id),
        "point_code": str(point_code),
        "device_code": str(device_code or ""),
        "value": value,
        "severity": str(severity or "warning"),
        "status": status,
        "message": str(message or ""),
        "fired_at": fired_at or _utc_now_iso(),
    }


def make_config_applied(
    *,
    edge_id: str,
    version: int,
    status: str,
    error: str | None = None,
) -> Dict[str, Any]:
    """Edge → center: result of applying an ``apply_config`` snapshot."""
    if status not in (CONFIG_APPLIED_OK, CONFIG_APPLIED_ERROR):
        raise ValueError(f"invalid config_applied status: {status!r}")
    frame: Dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "type": FRAME_CONFIG_APPLIED,
        "edge_id": edge_id,
        "version": int(version),
        "status": status,
    }
    if error:
        frame["error"] = str(error)
    return frame


def make_task_state(
    *,
    edge_id: str,
    task_id: int,
    task_code: str,
    state: str,
    error: str | None = None,
) -> Dict[str, Any]:
    """Edge → center: report a task lifecycle transition (v0.2).

    Retained so a v0.2 edge keeps working against a v0.3 center. v0.3
    edges emit :func:`make_lifecycle` instead.
    """
    if state not in TASK_STATES:
        raise ValueError(f"invalid task_state: {state!r}")
    frame: Dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "type": FRAME_TASK_STATE,
        "edge_id": edge_id,
        "task_id": int(task_id),
        "task_code": task_code,
        "state": state,
    }
    if error:
        frame["error"] = str(error)
    return frame


def make_lifecycle(
    *,
    edge_id: str,
    monotonic_seq: int,
    event: str,
    task_id: int | None = None,
    task_code: str | None = None,
    error: str | None = None,
    ts: str | None = None,
) -> Dict[str, Any]:
    """Edge → center: a session- or task-level lifecycle event (v0.3).

    ``event`` must be one of :data:`LIFECYCLE_EVENTS`. ``task_id`` /
    ``task_code`` are required for every ``task.*`` event and rejected for
    session-scoped events.
    """
    if event not in LIFECYCLE_EVENTS:
        raise ValueError(f"invalid lifecycle event: {event!r}")
    is_task = event.startswith(LIFECYCLE_TASK_PREFIX)
    if is_task and (task_id is None or task_code is None):
        raise ValueError(f"lifecycle event {event!r} requires task_id/task_code")
    frame: Dict[str, Any] = {
        "v": PROTOCOL_VERSION,
        "type": FRAME_LIFECYCLE,
        "edge_id": edge_id,
        "monotonic_seq": int(monotonic_seq),
        "event": event,
        "ts": ts or _utc_now_iso(),
    }
    if is_task:
        frame["task_id"] = int(task_id)
        frame["task_code"] = str(task_code)
    if error:
        frame["error"] = str(error)
    return frame


def task_state_to_lifecycle_event(state: str) -> str:
    """Map a v0.2 ``task_state`` string to its v0.3 ``lifecycle`` event."""
    try:
        return LIFECYCLE_TASK_EVENTS[state]
    except KeyError as exc:
        raise ValueError(f"invalid task state: {state!r}") from exc


def make_sample_batch(
    *,
    edge_id: str,
    monotonic_seq: int,
    task_id: int,
    task_code: str,
    samples: List[Dict[str, Any]],
    window_start: str,
    window_end: str,
) -> Dict[str, Any]:
    """Edge → center: one aggregation window of sampled point values (v0.3).

    ``samples`` is a list of ``{point_code, value, quality, timestamp}``
    dicts; it may be empty for a quiet window.
    """
    return {
        "v": PROTOCOL_VERSION,
        "type": FRAME_SAMPLE_BATCH,
        "edge_id": edge_id,
        "monotonic_seq": int(monotonic_seq),
        "task_id": int(task_id),
        "task_code": str(task_code),
        "window_start": str(window_start),
        "window_end": str(window_end),
        "samples": list(samples),
    }
