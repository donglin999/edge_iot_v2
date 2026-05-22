"""Distributed control-plane wire protocol — frame helpers.

Single source of truth for the JSON frames exchanged between the center
and each `edge-agent` over `/ws/fleet/`. The protocol doc lives in
``docs/distributed/protocol.md`` and is authoritative.

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

PROTOCOL_VERSION = "0.3"
# Versions the center is willing to talk to. Add older majors here when
# an upgrade boundary needs explicit shim support.
ACCEPTED_PROTOCOL_VERSIONS = frozenset({"0.1", "0.2", "0.3"})

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


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def make_register(*, edge_id: str, token: str, version: str, labels: Dict[str, Any] | None = None) -> Dict[str, Any]:
    return {
        "v": PROTOCOL_VERSION,
        "type": FRAME_REGISTER,
        "edge_id": edge_id,
        "token": token,
        "version": version,
        "labels": labels or {},
    }


def make_heartbeat(*, edge_id: str, cpu: float = 0.0, mem: float = 0.0, uptime: float = 0.0, tasks: int = 0) -> Dict[str, Any]:
    return {
        "v": PROTOCOL_VERSION,
        "type": FRAME_HEARTBEAT,
        "edge_id": edge_id,
        "cpu": cpu,
        "mem": mem,
        "uptime": uptime,
        "tasks": tasks,
    }


def make_ack(*, ref: str | None = None) -> Dict[str, Any]:
    return {"v": PROTOCOL_VERSION, "type": FRAME_ACK, "ref": ref}


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
) -> Dict[str, Any]:
    """Center → edge: full configuration snapshot for the edge to apply.

    ``version`` is a monotonically-increasing integer per edge — the edge
    persists it as ``last_applied_version`` so a redelivered or stale frame
    can be rejected by comparison.
    """
    return {
        "v": PROTOCOL_VERSION,
        "type": FRAME_APPLY_CONFIG,
        "version": int(version),
        "tasks": list(tasks),
        "devices": list(devices),
        "points": list(points),
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
