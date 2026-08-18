#!/usr/bin/env python3
"""Verify the checksum and exact linux/amd64 contents of images.tar."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import tarfile
from pathlib import Path, PurePosixPath


EXPECTED_TAGS = {
    "edge-iot/backend:offline-amd64",
    "edge-iot/web:offline-amd64",
    "redis:7.0.10",
}
MAX_CHECKSUM_BYTES = 4096
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_CONFIG_BYTES = 16 * 1024 * 1024


class ArchiveError(Exception):
    pass


def _identity(file_stat: os.stat_result) -> tuple[int, ...]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _open_regular(path: Path) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    file_stat = os.fstat(descriptor)
    if not stat.S_ISREG(file_stat.st_mode):
        os.close(descriptor)
        raise ArchiveError(f"{path.name} is not a regular file")
    path_stat = os.stat(path, follow_symlinks=False)
    if (path_stat.st_dev, path_stat.st_ino) != (file_stat.st_dev, file_stat.st_ino):
        os.close(descriptor)
        raise ArchiveError(f"{path.name} changed while it was opened")
    return descriptor, file_stat


def _read_checksum(path: Path) -> str:
    descriptor, file_stat = _open_regular(path)
    try:
        if file_stat.st_size > MAX_CHECKSUM_BYTES:
            raise ArchiveError("images.tar.sha256 is unexpectedly large")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, MAX_CHECKSUM_BYTES + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_CHECKSUM_BYTES:
                raise ArchiveError("images.tar.sha256 is unexpectedly large")
        after = os.fstat(descriptor)
        path_after = os.stat(path, follow_symlinks=False)
        if (
            _identity(after) != _identity(file_stat)
            or _identity(path_after) != _identity(file_stat)
        ):
            raise ArchiveError("images.tar.sha256 changed while it was read")
    finally:
        os.close(descriptor)
    content = b"".join(chunks)
    try:
        lines = content.decode("ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise ArchiveError("images.tar.sha256 must be ASCII") from exc
    if len(lines) != 1:
        raise ArchiveError("images.tar.sha256 must contain exactly one record")
    match = re.fullmatch(r"([0-9a-fA-F]{64})[ \t]+\*?images\.tar", lines[0])
    if not match:
        raise ArchiveError("images.tar.sha256 must reference exactly images.tar")
    return match.group(1).lower()


def _member_bytes(archive: tarfile.TarFile, name: str, maximum: int) -> bytes:
    matches = [member for member in archive.getmembers() if member.name == name]
    if len(matches) != 1 or not matches[0].isfile():
        raise ArchiveError(f"archive must contain one regular {name}")
    if matches[0].size > maximum:
        raise ArchiveError(f"{name} exceeds its size limit")
    stream = archive.extractfile(matches[0])
    if stream is None:
        raise ArchiveError(f"cannot read {name}")
    content = stream.read(maximum + 1)
    if len(content) > maximum:
        raise ArchiveError(f"{name} exceeds its size limit")
    return content


def _safe_member_name(name: object) -> str:
    if not isinstance(name, str):
        raise ArchiveError("image config path must be a string")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or len(path.parts) != 1:
        raise ArchiveError("image config path is unsafe")
    return name


def verify(archive_path: Path, checksum_path: Path) -> None:
    expected_digest = _read_checksum(checksum_path)
    descriptor, before = _open_regular(archive_path)
    try:
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after_hash = os.fstat(descriptor)
        if _identity(before) != _identity(after_hash):
            raise ArchiveError("images.tar changed while hashing")
        if digest.hexdigest() != expected_digest:
            raise ArchiveError("images.tar SHA-256 mismatch")

        os.lseek(descriptor, 0, os.SEEK_SET)
        with os.fdopen(os.dup(descriptor), "rb") as archive_file:
            with tarfile.open(fileobj=archive_file, mode="r:*") as archive:
                try:
                    manifest = json.loads(
                        _member_bytes(archive, "manifest.json", MAX_MANIFEST_BYTES)
                    )
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ArchiveError("manifest.json is invalid JSON") from exc
                if not isinstance(manifest, list) or len(manifest) != len(EXPECTED_TAGS):
                    raise ArchiveError("manifest.json must contain exactly three image entries")

                seen: dict[str, dict[str, object]] = {}
                seen_configs: set[str] = set()
                for entry in manifest:
                    if not isinstance(entry, dict):
                        raise ArchiveError("manifest entry must be an object")
                    tags = entry.get("RepoTags")
                    if not isinstance(tags, list) or len(tags) != 1:
                        raise ArchiveError("every image must have exactly one RepoTag")
                    config_name = _safe_member_name(entry.get("Config"))
                    if config_name in seen_configs:
                        raise ArchiveError("every image must have a distinct config")
                    seen_configs.add(config_name)
                    try:
                        config = json.loads(
                            _member_bytes(archive, config_name, MAX_CONFIG_BYTES)
                        )
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise ArchiveError(f"{config_name} is invalid JSON") from exc
                    if not isinstance(config, dict):
                        raise ArchiveError(f"{config_name} must contain an image config object")
                    if config.get("architecture") != "amd64" or config.get("os") != "linux":
                        raise ArchiveError(f"{config_name} is not linux/amd64")
                    for tag in tags:
                        if not isinstance(tag, str) or tag in seen:
                            raise ArchiveError("image tags must be unique strings")
                        seen[tag] = config

                actual_tags = set(seen)
                if actual_tags != EXPECTED_TAGS:
                    missing = sorted(EXPECTED_TAGS - actual_tags)
                    extra = sorted(actual_tags - EXPECTED_TAGS)
                    raise ArchiveError(
                        f"unexpected image tag set (missing={missing}, extra={extra})"
                    )

        after_tar = os.fstat(descriptor)
        path_after = os.stat(archive_path, follow_symlinks=False)
        identity = _identity(before)
        if _identity(after_tar) != identity:
            raise ArchiveError("images.tar changed while reading its manifest")
        if _identity(path_after) != identity:
            raise ArchiveError("images.tar path was replaced during verification")
    finally:
        os.close(descriptor)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: verify-image-archive.py images.tar images.tar.sha256", file=sys.stderr)
        return 2
    try:
        verify(Path(argv[1]), Path(argv[2]))
    except (ArchiveError, OSError, tarfile.TarError) as exc:
        print(f"!! offline image archive verification failed: {exc}", file=sys.stderr)
        return 2
    print("[OK] images.tar checksum, exact tags and linux/amd64 configs verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
