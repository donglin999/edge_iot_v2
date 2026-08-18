#!/usr/bin/env python3
"""Create, verify, and precisely clean private DinD TLS client material."""
from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path
from typing import Optional, Sequence


REQUIRED_FILES = ("ca.pem", "cert.pem", "key.pem")
MAX_PEM_BYTES = 128 * 1024


class TLSMaterialError(RuntimeError):
    """DinD client TLS material is not private, regular, or identity-bound."""


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
    path: Path, descriptor: int, expected: tuple[int, int]
) -> None:
    descriptor_stat = os.fstat(descriptor)
    path_stat = os.lstat(path)
    if (
        not stat.S_ISDIR(descriptor_stat.st_mode)
        or stat.S_ISLNK(path_stat.st_mode)
        or not stat.S_ISDIR(path_stat.st_mode)
        or _identity(descriptor_stat) != expected
        or _identity(path_stat) != expected
        or descriptor_stat.st_uid != os.geteuid()
        or stat.S_IMODE(descriptor_stat.st_mode) != 0o700
    ):
        raise TLSMaterialError("DinD client TLS directory identity changed")


def create_directory(path_value: str, allowed_root_value: str) -> tuple[int, int]:
    path = Path(path_value)
    allowed_root = Path(allowed_root_value)
    if not path.is_absolute() or not allowed_root.is_absolute():
        raise TLSMaterialError("DinD TLS paths must be absolute")
    root_stat = os.lstat(allowed_root)
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise TLSMaterialError("DinD TLS allowed root must be a real directory")
    if path.parent.resolve(strict=True) != allowed_root.resolve(strict=True):
        raise TLSMaterialError("DinD TLS directory must be a direct child of allowed root")
    try:
        os.mkdir(path, 0o700)
    except FileExistsError as exc:
        raise TLSMaterialError("DinD TLS directory already exists") from exc
    descriptor = os.open(path, _directory_flags())
    try:
        identity = _identity(os.fstat(descriptor))
        verify_directory(path, descriptor, identity)
        return identity
    finally:
        os.close(descriptor)


def _open_verified_directory(
    path: Path, expected: tuple[int, int]
) -> int:
    descriptor = os.open(path, _directory_flags())
    try:
        verify_directory(path, descriptor, expected)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def secure_client_files(path: Path, expected: tuple[int, int]) -> None:
    directory = _open_verified_directory(path, expected)
    try:
        names = sorted(os.listdir(directory))
        if names != sorted(REQUIRED_FILES):
            raise TLSMaterialError("DinD client TLS directory has an unexpected file set")
        for name in REQUIRED_FILES:
            entry_stat = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(entry_stat.st_mode):
                raise TLSMaterialError(f"DinD TLS client file is not regular: {name}")
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=directory,
            )
            try:
                opened = os.fstat(descriptor)
                if _identity(opened) != _identity(entry_stat):
                    raise TLSMaterialError(f"DinD TLS client file changed: {name}")
                if opened.st_size <= 0 or opened.st_size > MAX_PEM_BYTES:
                    raise TLSMaterialError(f"DinD TLS client file size is invalid: {name}")
                os.fchmod(descriptor, 0o600)
                chunks = []
                offset = 0
                while offset < opened.st_size:
                    block = os.pread(
                        descriptor,
                        min(16 * 1024, opened.st_size - offset),
                        offset,
                    )
                    if not block:
                        raise TLSMaterialError(
                            f"DinD TLS client file ended during verification: {name}"
                        )
                    chunks.append(block)
                    offset += len(block)
                payload = b"".join(chunks)
                if b"-----BEGIN " not in payload or b"-----END " not in payload:
                    raise TLSMaterialError(f"DinD TLS client file is not PEM: {name}")
                final = os.fstat(descriptor)
                final_path = os.stat(
                    name, dir_fd=directory, follow_symlinks=False
                )
                if (
                    _identity(final) != _identity(opened)
                    or _identity(final_path) != _identity(opened)
                    or final.st_size != opened.st_size
                    or final.st_mtime_ns != opened.st_mtime_ns
                ):
                    raise TLSMaterialError(f"DinD TLS client file changed: {name}")
            finally:
                os.close(descriptor)
        os.fsync(directory)
        verify_directory(path, directory, expected)
    finally:
        os.close(directory)


def cleanup_directory(path: Path, expected: tuple[int, int]) -> None:
    try:
        directory = _open_verified_directory(path, expected)
    except FileNotFoundError:
        return
    try:
        for name in os.listdir(directory):
            entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if not (stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode)):
                raise TLSMaterialError("refusing unexpected DinD TLS cleanup entry")
            os.unlink(name, dir_fd=directory)
        os.fsync(directory)
        verify_directory(path, directory, expected)
    finally:
        os.close(directory)
    current = os.lstat(path)
    if _identity(current) != expected:
        raise TLSMaterialError("DinD TLS directory changed before cleanup")
    os.rmdir(path)


def _parse_identity(device: str, inode: str) -> tuple[int, int]:
    try:
        return int(device), int(inode)
    except ValueError as exc:
        raise TLSMaterialError("DinD TLS identity must be numeric") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--path", required=True)
    create.add_argument("--allowed-root", required=True)
    for name in ("secure", "cleanup"):
        command = commands.add_parser(name)
        command.add_argument("--path", required=True)
        command.add_argument("--device", required=True)
        command.add_argument("--inode", required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "create":
            identity = create_directory(args.path, args.allowed_root)
            print(identity[0], identity[1])
            return 0
        path = Path(args.path)
        identity = _parse_identity(args.device, args.inode)
        if args.command == "secure":
            secure_client_files(path, identity)
        else:
            cleanup_directory(path, identity)
    except (OSError, TLSMaterialError) as exc:
        print(f"SAFETY ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
