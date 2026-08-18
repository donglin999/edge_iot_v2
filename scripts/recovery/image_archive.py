#!/usr/bin/env python3
"""Create and load a SHA-bound Docker image archive through held file descriptors."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional, Sequence


IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ArchiveError(RuntimeError):
    """An archive identity, checksum, Docker, or publication check failed."""


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _validate_parent(path: Path) -> tuple[int, int]:
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ArchiveError("archive parent must be a real directory")
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise ArchiveError("archive parent must be owner-controlled and not group/world writable")
    return _identity(metadata)


def _validate_new_file(path_value: str, suffix: str) -> Path:
    path = Path(os.path.abspath(Path(path_value).expanduser()))
    if path.suffix != suffix:
        raise ArchiveError(f"destination must end in {suffix}")
    if path in {Path("/"), Path.home().resolve(), Path.cwd().resolve()} or path.parent == Path("/"):
        raise ArchiveError("refusing broad archive destination")
    if path.exists() or path.is_symlink():
        raise ArchiveError("refusing to overwrite archive output")
    _validate_parent(path.parent)
    return path


def _open_regular(path: Path) -> tuple[int, tuple[int, int]]:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    metadata = os.fstat(descriptor)
    current = os.lstat(path)
    if not stat.S_ISREG(metadata.st_mode) or _identity(metadata) != _identity(current):
        os.close(descriptor)
        raise ArchiveError("archive path did not bind to one regular file")
    return descriptor, _identity(metadata)


def _sha256_fd(descriptor: int, expected: tuple[int, int]) -> str:
    before = os.fstat(descriptor)
    if _identity(before) != expected:
        raise ArchiveError("held archive descriptor changed identity")
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        block = os.read(descriptor, 1024 * 1024)
        if not block:
            break
        digest.update(block)
    after = os.fstat(descriptor)
    if _identity(after) != expected or after.st_size != before.st_size:
        raise ArchiveError("archive changed while hashing")
    return digest.hexdigest()


def _write_all(descriptor: int, payload: bytes) -> None:
    written = 0
    while written < len(payload):
        count = os.write(descriptor, payload[written:])
        if count <= 0:
            raise ArchiveError("could not write checksum document")
        written += count


def _run(
    command: list[str],
    *,
    stdin: Optional[int] = None,
    stdout: Optional[int] = None,
    timeout: float,
) -> None:
    try:
        completed = subprocess.run(
            command,
            stdin=stdin,
            stdout=stdout if stdout is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ArchiveError(f"Docker command could not complete: {type(exc).__name__}") from None
    if completed.returncode != 0:
        raise ArchiveError("Docker command failed; output suppressed")


def _publish_held_file(stage: Path, descriptor: int, expected: tuple[int, int], target: Path) -> None:
    if _identity(os.fstat(descriptor)) != expected or _identity(os.lstat(stage)) != expected:
        raise ArchiveError("staging file identity changed before publication")
    try:
        os.link(stage, target, follow_symlinks=False)
    except FileExistsError as exc:
        raise ArchiveError("refusing to overwrite archive output") from exc
    if _identity(os.lstat(target)) != expected:
        raise ArchiveError("published file identity does not match held staging file")
    directory = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def save_archive(args: argparse.Namespace) -> dict[str, object]:
    image_ids = tuple(args.image_id)
    if len(image_ids) != 3 or len(set(image_ids)) != 3:
        raise ArchiveError("save requires exactly three distinct immutable image IDs")
    if any(IMAGE_ID_RE.fullmatch(value) is None for value in image_ids):
        raise ArchiveError("invalid immutable image ID")
    archive = _validate_new_file(args.path, ".tar")
    checksum = _validate_new_file(args.checksum, ".json")
    if archive.parent != checksum.parent:
        raise ArchiveError("archive and checksum must share one private parent")
    parent_identity = _validate_parent(archive.parent)

    descriptor, temporary_name = tempfile.mkstemp(prefix=".m0-images-", suffix=".tar", dir=archive.parent)
    stage = Path(temporary_name)
    os.fchmod(descriptor, 0o600)
    stage_identity = _identity(os.fstat(descriptor))
    try:
        _run(
            [args.docker_bin, "image", "save", *image_ids],
            stdout=descriptor,
            timeout=args.timeout,
        )
        os.fsync(descriptor)
        if os.fstat(descriptor).st_size <= 0:
            raise ArchiveError("Docker produced an empty archive")
        digest = _sha256_fd(descriptor, stage_identity)
        if _validate_parent(archive.parent) != parent_identity:
            raise ArchiveError("archive parent changed during image save")
        _publish_held_file(stage, descriptor, stage_identity, archive)

        document = {
            "format_version": 1,
            "archive": archive.name,
            "archive_sha256": digest,
            "image_ids": list(image_ids),
        }
        checksum_descriptor, checksum_stage_name = tempfile.mkstemp(
            prefix=".m0-images-", suffix=".json", dir=archive.parent
        )
        checksum_stage = Path(checksum_stage_name)
        os.fchmod(checksum_descriptor, 0o600)
        checksum_identity = _identity(os.fstat(checksum_descriptor))
        try:
            payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
            _write_all(checksum_descriptor, payload)
            os.fsync(checksum_descriptor)
            _publish_held_file(
                checksum_stage, checksum_descriptor, checksum_identity, checksum
            )
        finally:
            os.close(checksum_descriptor)
        return {"operation": "save", "archive_sha256": digest, "image_ids": list(image_ids)}
    finally:
        os.close(descriptor)


def load_archive(args: argparse.Namespace) -> dict[str, object]:
    archive = Path(os.path.abspath(args.path))
    checksum = Path(os.path.abspath(args.checksum))
    if archive.parent != checksum.parent:
        raise ArchiveError("archive and checksum must share one parent")
    parent_identity = _validate_parent(archive.parent)
    archive_fd, archive_identity = _open_regular(archive)
    checksum_fd, checksum_identity = _open_regular(checksum)
    try:
        checksum_digest = _sha256_fd(checksum_fd, checksum_identity)
        os.lseek(checksum_fd, 0, os.SEEK_SET)
        raw = b""
        while True:
            block = os.read(checksum_fd, 64 * 1024)
            if not block:
                break
            raw += block
            if len(raw) > 1024 * 1024:
                raise ArchiveError("checksum document is too large")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArchiveError("checksum document is invalid") from exc
        image_ids = document.get("image_ids")
        if (
            document.get("format_version") != 1
            or document.get("archive") != archive.name
            or not isinstance(image_ids, list)
            or len(image_ids) != 3
            or len(set(image_ids)) != 3
            or any(not isinstance(value, str) or IMAGE_ID_RE.fullmatch(value) is None for value in image_ids)
        ):
            raise ArchiveError("checksum document scope is invalid")
        digest = _sha256_fd(archive_fd, archive_identity)
        if digest != document.get("archive_sha256"):
            raise ArchiveError("archive checksum mismatch")
        os.lseek(archive_fd, 0, os.SEEK_SET)
        command = [args.docker_bin]
        if args.docker_host:
            command += ["--host", args.docker_host]
        command += ["image", "load"]
        _run(command, stdin=archive_fd, timeout=args.timeout)
        for image_id in image_ids:
            inspect = [args.docker_bin]
            if args.docker_host:
                inspect += ["--host", args.docker_host]
            inspect += ["image", "inspect", image_id]
            _run(inspect, timeout=args.timeout)
        if _sha256_fd(archive_fd, archive_identity) != digest:
            raise ArchiveError("archive changed during Docker load")
        if _identity(os.lstat(archive)) != archive_identity:
            raise ArchiveError("archive path changed during Docker load")
        if _identity(os.lstat(checksum)) != checksum_identity:
            raise ArchiveError("checksum path changed during Docker load")
        if _sha256_fd(checksum_fd, checksum_identity) != checksum_digest:
            raise ArchiveError("checksum document changed during Docker load")
        if _validate_parent(archive.parent) != parent_identity:
            raise ArchiveError("archive parent changed during Docker load")
        return {"operation": "load", "archive_sha256": digest, "image_ids": image_ids}
    finally:
        os.close(checksum_fd)
        os.close(archive_fd)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("save", "load"):
        child = subparsers.add_parser(command)
        child.add_argument("--path", required=True)
        child.add_argument("--checksum", required=True)
        child.add_argument("--docker-bin", default="docker")
        child.add_argument("--timeout", type=float, default=600.0)
        if command == "save":
            child.add_argument("--image-id", action="append", required=True)
        else:
            child.add_argument("--docker-host")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        print("ERROR: timeout must be finite and positive", file=sys.stderr)
        return 2
    try:
        result = save_archive(args) if args.command == "save" else load_archive(args)
    except (ArchiveError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
