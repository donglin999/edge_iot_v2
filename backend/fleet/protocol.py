"""Distributed control-plane wire protocol — frame helpers.

Single source of truth for the JSON frames exchanged between the center
and each `edge-agent` over `/ws/fleet/`. The protocol doc lives in
``docs/distributed/protocol.md`` and is authoritative.

v0.2 (M2) adds the config-push channel:

- ``apply_config``   center → edge  full task/device/point snapshot
- ``config_applied`` edge   → center result of applying a snapshot
- ``task_state``     edge   → center per-task lifecycle state

Older v0.1 frames (``register``/``heartbeat``/``ack``/``error``) carry
unchanged semantics; the center accepts both ``v=0.1`` and ``v=0.2`` on
register so a stale agent can still connect during a rolling upgrade.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable

PROTOCOL_VERSION = "0.2"
# Versions the center is willing to talk to. Add older majors here when
# an upgrade boundary needs explicit shim support.
ACCEPTED_PROTOCOL_VERSIONS = frozenset({"0.1", "0.2"})

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
    """Edge → center: report a task lifecycle transition.

    ``task_id`` is the center-side AcqTask primary key (echoed back from
    ``apply_config.tasks[*].id``) so the center can update its assignment /
    EdgeTaskStatus rows without resolving ``task_code`` each time.
    ``task_code`` is included for human readability in logs.
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
