#!/usr/bin/env python3
"""Validate one exact Celery node's ping and active-queue JSON evidence."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence


class CelerySmokeError(RuntimeError):
    """A targeted Celery response was absent, ambiguous, or misconfigured."""


def _exact_node_response(path: Path, node: str) -> Any:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CelerySmokeError(f"invalid Celery JSON evidence: {path.name}") from exc
    if not isinstance(payload, dict) or set(payload) != {node}:
        raise CelerySmokeError(f"Celery response did not bind exactly to {node}")
    return payload[node]


def validate_reports(
    ping_path: Path,
    queue_path: Path,
    node: str,
    expected_queue: str,
) -> dict[str, str]:
    ping = _exact_node_response(ping_path, node)
    if ping != {"ok": "pong"}:
        raise CelerySmokeError(f"Celery node {node} did not return an exact pong")

    queues = _exact_node_response(queue_path, node)
    if (
        not isinstance(queues, list)
        or len(queues) != 1
        or not isinstance(queues[0], dict)
        or queues[0].get("name") != expected_queue
    ):
        raise CelerySmokeError(
            f"Celery node {node} is not bound only to queue {expected_queue}"
        )
    return {"node": node, "ping": "pong", "active_queue": expected_queue}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ping-report", type=Path, required=True)
    parser.add_argument("--queue-report", type=Path, required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--expected-queue", required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = validate_reports(
            args.ping_report,
            args.queue_report,
            args.node,
            args.expected_queue,
        )
    except CelerySmokeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
