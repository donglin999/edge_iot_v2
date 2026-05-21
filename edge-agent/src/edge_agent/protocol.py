"""Wire-protocol frame builders for the edge → center channel.

Mirrors `backend/fleet/protocol.py`. Kept duplicated (5 small functions)
so the edge-agent does not pull the whole Django backend just to encode
a JSON envelope. The protocol doc (`docs/distributed/protocol.md`) is
the source of truth — both copies must agree on PROTOCOL_VERSION.
"""
from __future__ import annotations

from typing import Any, Dict

PROTOCOL_VERSION = "0.1"

FRAME_REGISTER = "register"
FRAME_HEARTBEAT = "heartbeat"
FRAME_ERROR = "error"
FRAME_ACK = "ack"


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
