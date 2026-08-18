#!/usr/bin/env python3
"""Pin a protected env file to a private snapshot and verify its identity.

No configuration value is printed.  The receipt is a non-secret integrity record
used by load-and-up.sh to detect replacement or in-place modification.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import stat
import sys
from pathlib import Path


MAX_ENV_BYTES = 128 * 1024
ALLOWED_MODES = {0o400, 0o600}


class SafetyError(Exception):
    pass


def _flags(base: int) -> int:
    return base | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _validate_stat(file_stat: os.stat_result, *, label: str) -> None:
    mode = stat.S_IMODE(file_stat.st_mode)
    if not stat.S_ISREG(file_stat.st_mode):
        raise SafetyError(f"{label} is not a regular file")
    if file_stat.st_uid != os.geteuid():
        raise SafetyError(f"{label} owner must match the effective user")
    if mode not in ALLOWED_MODES:
        raise SafetyError(f"{label} mode must be 0400 or 0600")
    if file_stat.st_nlink != 1:
        raise SafetyError(f"{label} must have exactly one hard link")
    if file_stat.st_size > MAX_ENV_BYTES:
        raise SafetyError(f"{label} exceeds {MAX_ENV_BYTES} bytes")


def _stable_fields(file_stat: os.stat_result) -> tuple[int, ...]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_uid,
        stat.S_IMODE(file_stat.st_mode),
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _read_stable(fd: int, path: Path, *, label: str) -> tuple[bytes, os.stat_result]:
    before = os.fstat(fd)
    _validate_stat(before, label=label)
    path_before = os.stat(path, follow_symlinks=False)
    if _stable_fields(path_before) != _stable_fields(before):
        raise SafetyError(f"{label} path changed while it was opened")

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, min(8192, MAX_ENV_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_ENV_BYTES:
            raise SafetyError(f"{label} exceeds {MAX_ENV_BYTES} bytes")

    after = os.fstat(fd)
    path_after = os.stat(path, follow_symlinks=False)
    if _stable_fields(after) != _stable_fields(before):
        raise SafetyError(f"{label} changed while it was read")
    if _stable_fields(path_after) != _stable_fields(before):
        raise SafetyError(f"{label} path was replaced while it was read")
    return b"".join(chunks), after


def _receipt(file_stat: os.stat_result, content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()
    return ":".join(
        (
            str(file_stat.st_dev),
            str(file_stat.st_ino),
            str(file_stat.st_uid),
            format(stat.S_IMODE(file_stat.st_mode), "o"),
            str(file_stat.st_size),
            digest,
        )
    )


def prepare(source: Path, destination: Path) -> str:
    parent_stat = os.stat(destination.parent, follow_symlinks=False)
    if not stat.S_ISDIR(parent_stat.st_mode):
        raise SafetyError("snapshot parent is not a directory")
    if parent_stat.st_uid != os.geteuid() or stat.S_IMODE(parent_stat.st_mode) != 0o700:
        raise SafetyError("snapshot parent must be owned by the effective user with mode 0700")

    source_fd = os.open(source, _flags(os.O_RDONLY))
    try:
        content, _ = _read_stable(source_fd, source, label="source env file")
    finally:
        os.close(source_fd)

    destination_fd = os.open(
        destination,
        _flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
        0o600,
    )
    try:
        view = memoryview(content)
        while view:
            written = os.write(destination_fd, view)
            if written <= 0:
                raise SafetyError("failed to write env snapshot")
            view = view[written:]
        os.fsync(destination_fd)
        snapshot_stat = os.fstat(destination_fd)
    finally:
        os.close(destination_fd)

    _validate_stat(snapshot_stat, label="env snapshot")
    return _receipt(snapshot_stat, content)


def verify(path: Path, expected_receipt: str) -> None:
    descriptor = os.open(path, _flags(os.O_RDONLY))
    try:
        content, file_stat = _read_stable(descriptor, path, label="env snapshot")
    finally:
        os.close(descriptor)
    if not hmac.compare_digest(_receipt(file_stat, content), expected_receipt):
        raise SafetyError("env snapshot identity or content changed after validation")


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 4 and argv[1] == "prepare":
            print(prepare(Path(argv[2]), Path(argv[3])))
            return 0
        if len(argv) == 4 and argv[1] == "verify":
            verify(Path(argv[2]), argv[3])
            return 0
        raise SafetyError("usage: prepare-env.py prepare SOURCE DEST | verify SNAPSHOT RECEIPT")
    except (OSError, SafetyError) as exc:
        print(f"!! protected env safety check failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
