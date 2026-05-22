"""Wire-protocol frame builders for the edge → center channel.

Mirrors `backend/fleet/protocol.py`. Kept duplicated (small set of pure
helpers) so the edge-agent does not have to import the whole Django
backend just to encode a JSON envelope. The protocol doc
(`docs/distributed/protocol.md`) is authoritative — both copies must
agree on ``PROTOCOL_VERSION``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List

PROTOCOL_VERSION = "0.4"

# --- frame type constants ---------------------------------------------------

FRAME_REGISTER = "register"
FRAME_HEARTBEAT = "heartbeat"
FRAME_ERROR = "error"
FRAME_ACK = "ack"
FRAME_APPLY_CONFIG = "apply_config"
FRAME_CONFIG_APPLIED = "config_applied"
FRAME_TASK_STATE = "task_state"
# v0.3 uplink
FRAME_LIFECYCLE = "lifecycle"
FRAME_SAMPLE_BATCH = "sample_batch"
# v0.4 alarm uplink
FRAME_ALARM_EVENT = "alarm_event"

# --- v0.4 alarm event states ------------------------------------------------

ALARM_STATE_FIRING = "firing"
ALARM_STATE_CLEARED = "cleared"
ALARM_STATES = frozenset({ALARM_STATE_FIRING, ALARM_STATE_CLEARED})

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

LIFECYCLE_SESSION_ONLINE = "session.online"
LIFECYCLE_TASK_PREFIX = "task."
LIFECYCLE_TASK_EVENTS: Dict[str, str] = {
    TASK_STATE_STARTING: LIFECYCLE_TASK_PREFIX + TASK_STATE_STARTING,
    TASK_STATE_RUNNING: LIFECYCLE_TASK_PREFIX + TASK_STATE_RUNNING,
    TASK_STATE_STOPPING: LIFECYCLE_TASK_PREFIX + TASK_STATE_STOPPING,
    TASK_STATE_STOPPED: LIFECYCLE_TASK_PREFIX + TASK_STATE_STOPPED,
    TASK_STATE_ERROR: LIFECYCLE_TASK_PREFIX + TASK_STATE_ERROR,
}
LIFECYCLE_EVENTS: frozenset = frozenset(
    {LIFECYCLE_SESSION_ONLINE} | set(LIFECYCLE_TASK_EVENTS.values())
)


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


def make_config_applied(
    *,
    edge_id: str,
    version: int,
    status: str,
    error: str | None = None,
) -> Dict[str, Any]:
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
    """Edge → center: v0.2 task lifecycle transition.

    Retained for reference / tests. v0.3 edges emit :func:`make_lifecycle`.
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


def task_state_to_lifecycle_event(state: str) -> str:
    """Map a ``task_state`` string to its v0.3 ``lifecycle`` event name."""
    try:
        return LIFECYCLE_TASK_EVENTS[state]
    except KeyError as exc:
        raise ValueError(f"invalid task state: {state!r}") from exc


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
    """Edge → center: a session- or task-level lifecycle event (v0.3)."""
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
    """Edge → center: one aggregation window of sampled point values (v0.3)."""
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

    ``rule_id`` is the center-side ``AlarmRule.pk`` — rule definitions live
    at the center and are mirrored to the edge via ``apply_config``, so the
    pk is stable across both sides and lets the center re-attach the event.
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
