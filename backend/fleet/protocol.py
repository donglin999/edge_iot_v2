"""Distributed control-plane wire protocol — frame helpers.

Single source of truth for the JSON frames exchanged between the center
and each `edge-agent` over `/ws/fleet/`. v0.1 supports three frame types:

- ``register``  — first frame sent by the edge after the WS opens
- ``heartbeat`` — sent every ~1s while the connection is open
- ``error``     — center → edge, signals a fatal protocol/auth error

Every frame carries a top-level ``v`` field so we can evolve the schema
without breaking older edges (M2+ will add ``ack``/``cmd``/``sample``).
"""
from __future__ import annotations

from typing import Any, Dict

PROTOCOL_VERSION = "0.1"

FRAME_REGISTER = "register"
FRAME_HEARTBEAT = "heartbeat"
FRAME_ERROR = "error"
FRAME_ACK = "ack"

ERROR_AUTH = "auth_failed"
ERROR_BAD_FRAME = "bad_frame"
ERROR_UNKNOWN_EDGE = "unknown_edge"


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
