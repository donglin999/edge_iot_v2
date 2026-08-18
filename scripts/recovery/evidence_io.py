#!/usr/bin/env python3
"""Atomic evidence-directory guard and exclusive command-output capture."""
from __future__ import annotations

import argparse
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence

import safety


NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class EvidenceError(RuntimeError):
    """Evidence identity, creation, or exclusive-output validation failed."""


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def verify_directory(
    root: Path,
    descriptor: int,
    expected_identity: tuple[int, int],
) -> None:
    descriptor_stat = os.fstat(descriptor)
    path_stat = os.lstat(root)
    if (
        not stat.S_ISDIR(descriptor_stat.st_mode)
        or stat.S_ISLNK(path_stat.st_mode)
        or not stat.S_ISDIR(path_stat.st_mode)
        or _identity(descriptor_stat) != expected_identity
        or _identity(path_stat) != expected_identity
        or descriptor_stat.st_uid != os.geteuid()
        or stat.S_IMODE(descriptor_stat.st_mode) != 0o700
    ):
        raise EvidenceError("evidence directory identity, owner, or mode changed")


def create_evidence_directory(
    path_value: str,
    allowed_root_value: str,
    project: str,
) -> tuple[Path, int, tuple[int, int]]:
    try:
        path = safety.validate_new_evidence_path(
            path_value, allowed_root_value, project
        )
    except safety.SafetyError as exc:
        raise EvidenceError(str(exc)) from exc
    try:
        os.mkdir(path, 0o700)
    except FileExistsError as exc:
        raise EvidenceError("evidence directory was created concurrently") from exc
    descriptor: Optional[int] = None
    try:
        descriptor = os.open(path, _directory_flags())
        identity = _identity(os.fstat(descriptor))
        verify_directory(path, descriptor, identity)
        return path, descriptor, identity
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        raise


def _validate_output_name(name: str) -> str:
    if NAME_RE.fullmatch(name) is None or name in {".", ".."}:
        raise EvidenceError("evidence output name must be one plain filename")
    return name


def capture_output(
    root: Path,
    directory_descriptor: int,
    directory_identity: tuple[int, int],
    name: str,
    command: Sequence[str],
) -> int:
    verify_directory(root, directory_descriptor, directory_identity)
    name = _validate_output_name(name)
    if not command:
        raise EvidenceError("evidence capture command is required")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        output_descriptor = os.open(
            name, flags, 0o600, dir_fd=directory_descriptor
        )
    except FileExistsError as exc:
        raise EvidenceError(f"refusing existing evidence output: {name}") from exc
    try:
        initial = os.fstat(output_descriptor)
        if not stat.S_ISREG(initial.st_mode) or stat.S_IMODE(initial.st_mode) != 0o600:
            raise EvidenceError("exclusive evidence output is not a private regular file")
        try:
            completed = subprocess.run(
                list(command),
                stdin=None,
                stdout=output_descriptor,
                stderr=None,
                check=False,
            )
        except OSError as exc:
            raise EvidenceError(
                f"evidence command could not start: {type(exc).__name__}"
            ) from None
        os.fsync(output_descriptor)
        final = os.fstat(output_descriptor)
        path_stat = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(final.st_mode)
            or stat.S_IMODE(final.st_mode) != 0o600
            or _identity(final) != _identity(initial)
            or _identity(path_stat) != _identity(initial)
        ):
            raise EvidenceError("evidence output identity changed during capture")
        os.fsync(directory_descriptor)
        verify_directory(root, directory_descriptor, directory_identity)
        return completed.returncode
    finally:
        os.close(output_descriptor)


def _parse_identity(device: str, inode: str) -> tuple[int, int]:
    try:
        identity = int(device), int(inode)
    except ValueError as exc:
        raise EvidenceError("evidence identity must be numeric") from exc
    if min(identity) < 0:
        raise EvidenceError("evidence identity must be non-negative")
    return identity


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-and-exec")
    create.add_argument("--path", required=True)
    create.add_argument("--allowed-root", required=True)
    create.add_argument("--project", required=True)
    create.add_argument("--script", required=True)

    verify = commands.add_parser("verify")
    verify.add_argument("--root", required=True)
    verify.add_argument("--fd", required=True, type=int)
    verify.add_argument("--device", required=True)
    verify.add_argument("--inode", required=True)

    capture = commands.add_parser("capture")
    capture.add_argument("--root", required=True)
    capture.add_argument("--fd", required=True, type=int)
    capture.add_argument("--device", required=True)
    capture.add_argument("--inode", required=True)
    capture.add_argument("--name", required=True)
    capture.add_argument("remainder", nargs=argparse.REMAINDER)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "create-and-exec":
            path, descriptor, identity = create_evidence_directory(
                args.path, args.allowed_root, args.project
            )
            script = Path(args.script).resolve(strict=True)
            if not script.is_file():
                raise EvidenceError("drill script must be a regular file")
            environment = os.environ.copy()
            environment.update(
                {
                    "M0_EVIDENCE_READY": "1",
                    "M0_EVIDENCE_GUARD_PID": str(os.getpid()),
                    "M0_EVIDENCE_DIR_FD": str(descriptor),
                    "M0_EVIDENCE_DEVICE": str(identity[0]),
                    "M0_EVIDENCE_INODE": str(identity[1]),
                }
            )
            os.set_inheritable(descriptor, True)
            os.execve(
                script,
                [
                    str(script),
                    "--project",
                    args.project,
                    "--evidence",
                    str(path),
                    "--allowed-root",
                    args.allowed_root,
                ],
                environment,
            )
        identity = _parse_identity(args.device, args.inode)
        root = Path(args.root)
        verify_directory(root, args.fd, identity)
        if args.command == "verify":
            return 0
        command = list(args.remainder)
        if command[:1] == ["--"]:
            command = command[1:]
        return capture_output(root, args.fd, identity, args.name, command)
    except (EvidenceError, OSError) as exc:
        print(f"SAFETY ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
