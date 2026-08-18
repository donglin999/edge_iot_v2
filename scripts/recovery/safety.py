#!/usr/bin/env python3
"""Fail-closed safety checks for the disposable M0 recovery drill."""
from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from pathlib import Path
from typing import Iterable, Optional, Sequence


PROJECT_RE = re.compile(r"^m0ci-[a-z0-9](?:[a-z0-9-]{6,46}[a-z0-9])$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
FORBIDDEN_PROJECTS = {
    "edge_iot_v2",
    "edge-iot-v2",
    "edge_iot_v2-remediation",
    "edge-iot",
    "production",
}


class SafetyError(RuntimeError):
    """The drill cannot prove that its target is disposable and isolated."""


def validate_project(project: str) -> str:
    if project in FORBIDDEN_PROJECTS or PROJECT_RE.fullmatch(project) is None:
        raise SafetyError(
            "project must be a unique disposable name matching "
            "m0ci-[a-z0-9-] (9-49 characters)"
        )
    return project


def validate_image_ids(values: Iterable[str]) -> tuple[str, ...]:
    image_ids = tuple(values)
    if not image_ids or any(IMAGE_ID_RE.fullmatch(value) is None for value in image_ids):
        raise SafetyError("every image reference must be an immutable sha256 ID")
    return image_ids


def _is_real_directory(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)


def validate_new_evidence_path(path_value: str, allowed_root_value: str, project: str) -> Path:
    validate_project(project)
    path = Path(path_value)
    allowed_root = Path(allowed_root_value)
    if not path.is_absolute() or not allowed_root.is_absolute():
        raise SafetyError("evidence and allowed root must be absolute paths")
    if not _is_real_directory(allowed_root):
        raise SafetyError("allowed evidence root must be a real existing directory")
    resolved_root = allowed_root.resolve(strict=True)
    if resolved_root in {Path("/"), Path.home().resolve(), Path.cwd().resolve()}:
        raise SafetyError("refusing a broad evidence root")
    if path.exists() or path.is_symlink():
        raise SafetyError("evidence path must not already exist")
    try:
        resolved_parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise SafetyError("evidence parent must already exist") from exc
    if resolved_parent != resolved_root:
        raise SafetyError("evidence must be a direct child of the allowed root")
    if project not in path.name:
        raise SafetyError("evidence directory name must contain the disposable project")
    return path


def validate_created_private_directory(path_value: str) -> Path:
    path = Path(path_value)
    if not path.is_absolute() or not _is_real_directory(path):
        raise SafetyError("evidence directory must be an absolute real directory")
    metadata = os.lstat(path)
    if metadata.st_uid != os.geteuid():
        raise SafetyError("evidence directory must be owned by the current user")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise SafetyError("evidence directory mode must be exactly 0700")
    return path


def validate_compose_source(path_value: str) -> Path:
    path = Path(path_value).resolve(strict=True)
    text = path.read_text(encoding="utf-8")
    forbidden = {
        "container_name:": "explicit container names",
        "network_mode:": "non-bridge network mode",
        "external: true": "external resources",
        "external: {": "external resources",
        "build:": "an in-drill image build",
        "/var/run/docker.sock": "the host Docker socket",
    }
    for marker, description in forbidden.items():
        if marker in text:
            raise SafetyError(f"Compose source contains {description}: {marker}")
    image_count = text.count("\n    image:")
    no_pull_count = text.count("\n    pull_policy: never")
    if image_count == 0 or no_pull_count != image_count:
        raise SafetyError("every drill image must be protected by pull_policy: never")
    published_count = text.count("\n        published:")
    loopback_count = text.count('\n        host_ip: "127.0.0.1"')
    if published_count == 0 or loopback_count != published_count:
        raise SafetyError("published ports must be explicitly bound to 127.0.0.1")
    if "driver: bridge" not in text or "internal: true" not in text:
        raise SafetyError("drill network must be an internal bridge")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    project = subparsers.add_parser("project")
    project.add_argument("value")

    images = subparsers.add_parser("images")
    images.add_argument("values", nargs="+")

    evidence = subparsers.add_parser("new-evidence")
    evidence.add_argument("--path", required=True)
    evidence.add_argument("--allowed-root", required=True)
    evidence.add_argument("--project", required=True)

    private = subparsers.add_parser("private-directory")
    private.add_argument("path")

    compose = subparsers.add_parser("compose")
    compose.add_argument("path")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "project":
            validate_project(args.value)
        elif args.command == "images":
            validate_image_ids(args.values)
        elif args.command == "new-evidence":
            validate_new_evidence_path(args.path, args.allowed_root, args.project)
        elif args.command == "private-directory":
            validate_created_private_directory(args.path)
        elif args.command == "compose":
            validate_compose_source(args.path)
        else:  # pragma: no cover - argparse makes this unreachable.
            raise SafetyError("unsupported safety check")
    except (OSError, SafetyError) as exc:
        print(f"SAFETY ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
