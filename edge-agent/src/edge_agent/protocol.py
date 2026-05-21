"""Wire-protocol frame builders for the edge → center channel.

Mirrors `backend/fleet/protocol.py`. Kept duplicated (small set of pure
helpers) so the edge-agent does not have to import the whole Django
backend just to encode a JSON envelope. The protocol doc
(`docs/distributed/protocol.md`) is authoritative — both copies must
agree on ``PROTOCOL_VERSION``.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable

PROTOCOL_VERSION = "0.2"

# --- frame type constants ---------------------------------------------------

FRAME_REGISTER = "register"
FRAME_HEARTBEAT = "heartbeat"
FRAME_ERROR = "error"
FRAME_ACK = "ack"
FRAME_APPLY_CONFIG = "apply_config"
FRAME_CONFIG_APPLIED = "config_applied"
FRAME_TASK_STATE = "task_state"

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
