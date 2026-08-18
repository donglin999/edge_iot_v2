#!/usr/bin/env python3
"""Safe InfluxDB 2.x backup/restore wrapper for an M0 recovery drill.

The wrapper requires every data-scope argument.  Credentials are read from a
0400/0600 token file and passed through ``INFLUX_TOKEN`` so they are not written
to shell history, process arguments, manifests, or logs.  Restore is limited to
a new bucket in the same organization; it never deletes or resets a bucket.
"""
from __future__ import annotations

import argparse
import ctypes
import csv
import errno
import hashlib
import io
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit


MANIFEST_NAME = "edge-iot-backup-manifest.json"
CHECKSUM_NAME = "edge-iot-backup-sha256.json"
MAX_TOKEN_BYTES = 64 * 1024
RFC3339_NANOSECONDS = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$"
)


class BackupError(RuntimeError):
    """A safety check, CLI command, or verification failed."""


def validate_name(value: str, label: str) -> str:
    value = value.strip()
    has_control_character = any(ord(character) < 32 for character in value)
    if not value or value in {"*", "-"} or has_control_character:
        raise BackupError(f"{label} must be an explicit non-wildcard value")
    return value


def validate_host(value: str) -> str:
    value = value.strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise BackupError("--host must be an explicit http:// or https:// URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise BackupError("--host must not contain credentials, a query, or a fragment")
    return value.rstrip("/")


def require_private_token_mode(mode: int, path: Path) -> None:
    if mode not in {0o400, 0o600}:
        raise BackupError(
            f"token file permissions must be exactly 0400 or 0600: {path}"
        )


def validate_token_file(path_value: str) -> Tuple[Path, str]:
    path = Path(path_value).expanduser().absolute()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise BackupError(f"could not securely open token file {path}: {exc}") from exc
    try:
        descriptor_stat = os.fstat(descriptor)
        path_stat = os.lstat(path)
        if stat.S_ISLNK(path_stat.st_mode) or (
            descriptor_stat.st_dev,
            descriptor_stat.st_ino,
        ) != (path_stat.st_dev, path_stat.st_ino):
            raise BackupError(f"token file changed during secure open: {path}")
        if not stat.S_ISREG(descriptor_stat.st_mode):
            raise BackupError(f"token file is not a regular file: {path}")
        mode = stat.S_IMODE(descriptor_stat.st_mode)
        require_private_token_mode(mode, path)
        chunks = []
        total = 0
        while total <= MAX_TOKEN_BYTES:
            chunk = os.read(descriptor, min(8192, MAX_TOKEN_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > MAX_TOKEN_BYTES:
            raise BackupError(f"token file exceeds {MAX_TOKEN_BYTES} bytes: {path}")
        try:
            token = b"".join(chunks).decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise BackupError(f"token file is not valid UTF-8: {path}") from exc
    finally:
        os.close(descriptor)
    if not token:
        raise BackupError(f"token file is empty: {path}")
    if len(token.splitlines()) != 1:
        raise BackupError(f"token file must contain exactly one token line: {path}")
    return path, token


def _broad_paths() -> set:
    return {Path("/"), Path.home().resolve(), Path.cwd().resolve()}


def _identity(file_stat: os.stat_result) -> Tuple[int, int]:
    return file_stat.st_dev, file_stat.st_ino


def validate_secure_archive_parent(path: Path) -> Tuple[int, int]:
    try:
        parent_stat = os.lstat(path)
    except OSError as exc:
        raise BackupError(f"could not inspect backup parent {path}: {exc}") from exc
    if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
        raise BackupError(f"backup parent must be a real directory: {path}")
    if parent_stat.st_uid != os.geteuid():
        raise BackupError(f"backup parent must be owned by the current user: {path}")
    if stat.S_IMODE(parent_stat.st_mode) & 0o022:
        raise BackupError(f"backup parent must not be group/world writable: {path}")
    return _identity(parent_stat)


def validate_new_archive_path(path_value: str) -> Path:
    raw = Path(path_value).expanduser()
    if raw.is_symlink():
        raise BackupError(f"backup path must not be a symlink: {raw}")
    path = Path(os.path.abspath(raw))
    if path in _broad_paths() or path.parent == Path("/"):
        raise BackupError(f"refusing broad backup path: {path}")
    if path.exists() or path.is_symlink():
        raise BackupError(f"backup path must not already exist: {path}")
    if not path.parent.exists() or not path.parent.is_dir():
        raise BackupError(f"backup parent must already exist: {path.parent}")
    validate_secure_archive_parent(path.parent)
    return path


def validate_archive_path(path_value: str) -> Path:
    path = Path(path_value).expanduser()
    if path.is_symlink():
        raise BackupError(f"backup path must not be a symlink: {path}")
    path = Path(os.path.abspath(path))
    if path in _broad_paths() or path.parent == Path("/"):
        raise BackupError(f"refusing broad backup path: {path}")
    try:
        path_stat = os.lstat(path)
    except OSError as exc:
        raise BackupError(f"backup path is not a directory: {path}") from exc
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
        raise BackupError(f"backup path is not a directory: {path}")
    validate_secure_archive_parent(path.parent)
    return path


def run_influx(
    influx_bin: str,
    arguments: Sequence[str],
    token: str,
    command_timeout: float,
) -> str:
    environment = os.environ.copy()
    environment["INFLUX_TOKEN"] = token
    try:
        completed = subprocess.run(
            [influx_bin, *arguments],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=command_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise BackupError(
            f"influx {arguments[0]} exceeded {command_timeout:g} seconds"
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or "influx command failed").strip().replace(
            token, "<redacted>"
        )
        raise BackupError(f"influx {arguments[0]} failed: {detail}")
    return completed.stdout or ""


def _flux_string(value: str) -> str:
    # JSON string escaping is compatible with Flux string literals.
    return json.dumps(value)


def _query_rows(raw_csv: str) -> Iterable[Mapping[str, str]]:
    header: Optional[List[str]] = None
    for row in csv.reader(io.StringIO(raw_csv)):
        if not row or (row[0].startswith("#")):
            continue
        if "_measurement" in row and "_field" in row:
            header = row
            continue
        if header is None:
            continue
        if len(row) < len(header):
            row += [""] * (len(header) - len(row))
        yield dict(zip(header, row))


def collect_verification_summary(
    influx_bin: str,
    host: str,
    org: str,
    bucket: str,
    token: str,
    command_timeout: float,
) -> Mapping[str, object]:
    base = (
        f"from(bucket: {_flux_string(bucket)}) "
        "|> range(start: time(v: 0)) "
        '|> group(columns: ["_measurement", "_field"]) '
    )
    queries = {
        "count": base
        + '|> count(column: "_value") '
        + '|> keep(columns: ["_measurement", "_field", "_value"])',
        "first": base
        + "|> first() "
        + '|> keep(columns: ["_measurement", "_field", "_time"])',
        "last": base
        + "|> last() "
        + '|> keep(columns: ["_measurement", "_field", "_time"])',
    }
    series: MutableMapping[Tuple[str, str], Dict[str, object]] = {}
    for metric, query in queries.items():
        output = run_influx(
            influx_bin,
            ["query", "--host", host, "--org", org, "--raw", query],
            token,
            command_timeout,
        )
        for row in _query_rows(output):
            measurement = row.get("_measurement", "")
            field = row.get("_field", "")
            if not measurement or not field:
                continue
            entry = series.setdefault(
                (measurement, field),
                {"measurement": measurement, "field": field},
            )
            if metric == "count":
                try:
                    entry[metric] = int(float(row.get("_value", "0")))
                except ValueError as exc:
                    raise BackupError("unexpected count returned by influx query") from exc
            else:
                entry[metric] = row.get("_time") or None

    normalized = []
    for key in sorted(series):
        entry = series[key]
        entry.setdefault("count", 0)
        entry.setdefault("first", None)
        entry.setdefault("last", None)
        if int(entry["count"]) <= 0 or entry["first"] is None or entry["last"] is None:
            raise BackupError(
                "incomplete count/first/last result returned by influx query"
            )
        normalized.append(entry)
    return {
        "series": normalized,
        "series_count": len(normalized),
        "field_value_count": sum(int(item["count"]) for item in normalized),
    }


def parse_rfc3339_nanoseconds(value: str) -> Tuple[datetime, int]:
    match = RFC3339_NANOSECONDS.fullmatch(value)
    if match is None:
        raise BackupError("archive verification time range is not RFC3339")
    base, fraction, offset = match.groups()
    normalized_offset = "+00:00" if offset == "Z" else offset
    try:
        whole_second = datetime.fromisoformat(base + normalized_offset)
    except ValueError as exc:
        raise BackupError("archive verification time range is not RFC3339") from exc
    nanoseconds = int((fraction or "0").ljust(9, "0"))
    return whole_second.astimezone(timezone.utc), nanoseconds


def validate_verification_summary(
    summary: object, allow_empty_snapshot: bool
) -> Mapping[str, object]:
    if not isinstance(summary, dict):
        raise BackupError("archive verification summary is missing or invalid")
    series = summary.get("series")
    series_count = summary.get("series_count")
    field_value_count = summary.get("field_value_count")
    if (
        not isinstance(series, list)
        or not isinstance(series_count, int)
        or isinstance(series_count, bool)
        or not isinstance(field_value_count, int)
        or isinstance(field_value_count, bool)
        or series_count != len(series)
    ):
        raise BackupError("archive verification summary is incomplete or inconsistent")
    keys = set()
    calculated_count = 0
    for item in series:
        if not isinstance(item, dict):
            raise BackupError("archive verification series entry is invalid")
        measurement = item.get("measurement")
        field = item.get("field")
        count = item.get("count")
        first = item.get("first")
        last = item.get("last")
        if (
            not isinstance(measurement, str)
            or not measurement
            or not isinstance(field, str)
            or not field
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count <= 0
            or not isinstance(first, str)
            or not first
            or not isinstance(last, str)
            or not last
        ):
            raise BackupError("archive verification series entry is incomplete")
        first_time = parse_rfc3339_nanoseconds(first)
        last_time = parse_rfc3339_nanoseconds(last)
        if first_time > last_time:
            raise BackupError("archive verification first time is after last time")
        key = (measurement, field)
        if key in keys:
            raise BackupError("archive verification summary contains duplicate series")
        keys.add(key)
        calculated_count += count
    if calculated_count != field_value_count:
        raise BackupError("archive field-value count is inconsistent")
    if (series_count == 0 or field_value_count == 0) and not allow_empty_snapshot:
        raise BackupError(
            "empty Influx verification summary is refused; "
            "use --allow-empty-snapshot only for an audited empty bucket"
        )
    return summary


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _regular_open_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _assert_directory_identity(path: Path, expected: Tuple[int, int]) -> None:
    try:
        path_stat = os.lstat(path)
    except OSError as exc:
        raise BackupError(f"archive directory changed or disappeared: {path}") from exc
    if not stat.S_ISDIR(path_stat.st_mode) or _identity(path_stat) != expected:
        raise BackupError(f"archive directory identity changed: {path}")


def _open_directory_no_follow(
    path: Path, expected: Optional[Tuple[int, int]] = None
) -> Tuple[int, Tuple[int, int]]:
    try:
        descriptor = os.open(path, _directory_open_flags())
    except OSError as exc:
        raise BackupError(f"could not securely open archive directory {path}: {exc}") from exc
    try:
        descriptor_stat = os.fstat(descriptor)
        if not stat.S_ISDIR(descriptor_stat.st_mode):
            raise BackupError(f"archive path is not a real directory: {path}")
        identity = _identity(descriptor_stat)
        _assert_directory_identity(path, identity)
        if expected is not None and identity != expected:
            raise BackupError(f"archive directory identity changed: {path}")
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, identity


def _hash_regular_descriptor(descriptor: int, display_name: str) -> str:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode):
        raise BackupError(f"archive contains a non-regular file: {display_name}")
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    after = os.fstat(descriptor)
    if (
        _identity(after) != _identity(before)
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise BackupError(f"archive file changed while hashing: {display_name}")
    return digest.hexdigest()


def _walk_archive_directory(
    directory_descriptor: int,
    prefix: str,
    set_permissions: bool,
    sync_files: bool,
) -> Dict[str, str]:
    try:
        with os.scandir(directory_descriptor) as iterator:
            names = sorted(entry.name for entry in iterator)
    except OSError as exc:
        raise BackupError(f"could not enumerate archive directory {prefix or '.'}: {exc}") from exc

    hashes: Dict[str, str] = {}
    for name in names:
        relative_name = f"{prefix}/{name}" if prefix else name
        try:
            entry_stat = os.stat(
                name, dir_fd=directory_descriptor, follow_symlinks=False
            )
        except OSError as exc:
            raise BackupError(f"archive entry changed: {relative_name}") from exc

        if stat.S_ISDIR(entry_stat.st_mode):
            try:
                child_descriptor = os.open(
                    name, _directory_open_flags(), dir_fd=directory_descriptor
                )
            except OSError as exc:
                raise BackupError(
                    f"could not securely open archive directory: {relative_name}"
                ) from exc
            try:
                child_stat = os.fstat(child_descriptor)
                child_identity = _identity(child_stat)
                if (
                    not stat.S_ISDIR(child_stat.st_mode)
                    or child_identity != _identity(entry_stat)
                ):
                    raise BackupError(
                        f"archive directory identity changed: {relative_name}"
                    )
                if set_permissions:
                    os.fchmod(child_descriptor, 0o700)
                hashes.update(
                    _walk_archive_directory(
                        child_descriptor,
                        relative_name,
                        set_permissions,
                        sync_files,
                    )
                )
                if sync_files:
                    os.fsync(child_descriptor)
                current_stat = os.stat(
                    name, dir_fd=directory_descriptor, follow_symlinks=False
                )
                if _identity(current_stat) != child_identity:
                    raise BackupError(
                        f"archive directory changed during traversal: {relative_name}"
                    )
            finally:
                os.close(child_descriptor)
            continue

        if not stat.S_ISREG(entry_stat.st_mode):
            raise BackupError(
                f"archive contains a symlink or special entry: {relative_name}"
            )
        try:
            file_descriptor = os.open(
                name, _regular_open_flags(), dir_fd=directory_descriptor
            )
        except OSError as exc:
            raise BackupError(
                f"could not securely open archive file: {relative_name}"
            ) from exc
        try:
            file_stat = os.fstat(file_descriptor)
            file_identity = _identity(file_stat)
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_identity != _identity(entry_stat)
            ):
                raise BackupError(f"archive file identity changed: {relative_name}")
            if set_permissions:
                os.fchmod(file_descriptor, 0o600)
            if sync_files:
                os.fsync(file_descriptor)
            digest = _hash_regular_descriptor(file_descriptor, relative_name)
            current_stat = os.stat(
                name, dir_fd=directory_descriptor, follow_symlinks=False
            )
            if _identity(current_stat) != file_identity:
                raise BackupError(
                    f"archive file changed during traversal: {relative_name}"
                )
        finally:
            os.close(file_descriptor)
        if relative_name != CHECKSUM_NAME:
            hashes[relative_name] = digest
    return hashes


def _copy_archive_directory(
    source_descriptor: int,
    destination_descriptor: int,
    prefix: str,
) -> None:
    """Copy only stable regular files/directories through no-follow dirfds."""
    try:
        with os.scandir(source_descriptor) as iterator:
            names = sorted(entry.name for entry in iterator)
    except OSError as exc:
        raise BackupError(
            f"could not enumerate restore source {prefix or '.'}: {exc}"
        ) from exc

    for name in names:
        if name in {"", ".", ".."} or "/" in name:
            raise BackupError("restore source contains an unsafe entry name")
        relative_name = f"{prefix}/{name}" if prefix else name
        try:
            source_entry_stat = os.stat(
                name, dir_fd=source_descriptor, follow_symlinks=False
            )
        except OSError as exc:
            raise BackupError(f"restore source entry changed: {relative_name}") from exc

        if stat.S_ISDIR(source_entry_stat.st_mode):
            source_child = None
            destination_child = None
            try:
                os.mkdir(name, 0o700, dir_fd=destination_descriptor)
                source_child = os.open(
                    name, _directory_open_flags(), dir_fd=source_descriptor
                )
                destination_child = os.open(
                    name, _directory_open_flags(), dir_fd=destination_descriptor
                )
            except OSError as exc:
                raise BackupError(
                    f"could not create private restore directory: {relative_name}"
                ) from exc
            try:
                assert source_child is not None and destination_child is not None
                source_child_stat = os.fstat(source_child)
                if (
                    not stat.S_ISDIR(source_child_stat.st_mode)
                    or _identity(source_child_stat) != _identity(source_entry_stat)
                ):
                    raise BackupError(
                        f"restore source directory identity changed: {relative_name}"
                    )
                _copy_archive_directory(
                    source_child, destination_child, relative_name
                )
                os.fchmod(destination_child, 0o500)
                os.fsync(destination_child)
                current_source_stat = os.stat(
                    name, dir_fd=source_descriptor, follow_symlinks=False
                )
                if _identity(current_source_stat) != _identity(source_child_stat):
                    raise BackupError(
                        f"restore source directory changed during copy: {relative_name}"
                    )
            finally:
                if destination_child is not None:
                    os.close(destination_child)
                if source_child is not None:
                    os.close(source_child)
            continue

        if not stat.S_ISREG(source_entry_stat.st_mode):
            raise BackupError(
                f"restore source contains a symlink or special entry: {relative_name}"
            )
        source_file = None
        destination_file = None
        try:
            source_file = os.open(
                name, _regular_open_flags(), dir_fd=source_descriptor
            )
            destination_flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            destination_file = os.open(
                name, destination_flags, 0o600, dir_fd=destination_descriptor
            )
        except OSError as exc:
            raise BackupError(
                f"could not create private restore file: {relative_name}"
            ) from exc
        try:
            assert source_file is not None and destination_file is not None
            source_before = os.fstat(source_file)
            if (
                not stat.S_ISREG(source_before.st_mode)
                or _identity(source_before) != _identity(source_entry_stat)
            ):
                raise BackupError(
                    f"restore source file identity changed: {relative_name}"
                )
            while True:
                chunk = os.read(source_file, 1024 * 1024)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_file, view)
                    view = view[written:]
            os.fchmod(destination_file, 0o400)
            os.fsync(destination_file)
            source_after = os.fstat(source_file)
            if (
                _identity(source_after) != _identity(source_before)
                or source_after.st_size != source_before.st_size
                or source_after.st_mtime_ns != source_before.st_mtime_ns
            ):
                raise BackupError(
                    f"restore source file changed during copy: {relative_name}"
                )
            current_source_stat = os.stat(
                name, dir_fd=source_descriptor, follow_symlinks=False
            )
            if _identity(current_source_stat) != _identity(source_before):
                raise BackupError(
                    f"restore source file name changed during copy: {relative_name}"
                )
        finally:
            if destination_file is not None:
                os.close(destination_file)
            if source_file is not None:
                os.close(source_file)


def _read_regular_at(
    directory_descriptor: int, name: str, maximum_bytes: int = 16 * 1024 * 1024
) -> bytes:
    try:
        descriptor = os.open(name, _regular_open_flags(), dir_fd=directory_descriptor)
    except OSError as exc:
        raise BackupError(f"archive metadata is missing or unsafe: {name}") from exc
    try:
        descriptor_stat = os.fstat(descriptor)
        identity = _identity(descriptor_stat)
        if not stat.S_ISREG(descriptor_stat.st_mode):
            raise BackupError(f"archive metadata is not a regular file: {name}")
        chunks: List[bytes] = []
        total = 0
        while total <= maximum_bytes:
            chunk = os.read(descriptor, min(65536, maximum_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > maximum_bytes:
            raise BackupError(f"archive metadata exceeds {maximum_bytes} bytes: {name}")
        current_stat = os.stat(
            name, dir_fd=directory_descriptor, follow_symlinks=False
        )
        if _identity(current_stat) != identity:
            raise BackupError(f"archive metadata changed while reading: {name}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _write_json_exclusive(
    directory_descriptor: int, name: str, payload: Mapping[str, object]
) -> None:
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=directory_descriptor)
    except OSError as exc:
        raise BackupError(f"could not create archive metadata {name}: {exc}") from exc
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        file_identity = _identity(os.fstat(descriptor))
        current_stat = os.stat(
            name, dir_fd=directory_descriptor, follow_symlinks=False
        )
        if not stat.S_ISREG(current_stat.st_mode) or _identity(current_stat) != file_identity:
            raise BackupError(f"archive metadata changed while writing: {name}")
    finally:
        os.close(descriptor)


def secure_archive_permissions(
    path: Path,
    expected_identity: Optional[Tuple[int, int]] = None,
    sync_files: bool = False,
) -> Tuple[int, int]:
    descriptor, identity = _open_directory_no_follow(path, expected_identity)
    try:
        os.fchmod(descriptor, 0o700)
        _walk_archive_directory(descriptor, "", True, sync_files)
        if sync_files:
            os.fsync(descriptor)
        _assert_directory_identity(path, identity)
    finally:
        os.close(descriptor)
    return identity


def _sha256(path: Path) -> str:
    try:
        descriptor = os.open(path, _regular_open_flags())
    except OSError as exc:
        raise BackupError(f"could not securely open archive file {path}: {exc}") from exc
    try:
        file_stat = os.fstat(descriptor)
        identity = _identity(file_stat)
        digest = _hash_regular_descriptor(descriptor, str(path))
        path_stat = os.lstat(path)
        if not stat.S_ISREG(path_stat.st_mode) or _identity(path_stat) != identity:
            raise BackupError(f"archive file identity changed: {path}")
        return digest
    finally:
        os.close(descriptor)


def write_archive_json(
    path: Path,
    name: str,
    payload: Mapping[str, object],
    expected_identity: Tuple[int, int],
) -> None:
    descriptor, identity = _open_directory_no_follow(path, expected_identity)
    try:
        _write_json_exclusive(descriptor, name, payload)
        os.fsync(descriptor)
        _assert_directory_identity(path, identity)
    finally:
        os.close(descriptor)


def write_checksums(path: Path, expected_identity: Tuple[int, int]) -> None:
    descriptor, identity = _open_directory_no_follow(path, expected_identity)
    try:
        checksums = _walk_archive_directory(descriptor, "", False, False)
        _write_json_exclusive(descriptor, CHECKSUM_NAME, {"sha256": checksums})
        os.fsync(descriptor)
        _assert_directory_identity(path, identity)
    finally:
        os.close(descriptor)


def verify_archive(
    path: Path,
    org: str,
    bucket: str,
    allow_empty_snapshot: bool = False,
    expected_identity: Optional[Tuple[int, int]] = None,
) -> Mapping[str, object]:
    descriptor, identity = _open_directory_no_follow(path, expected_identity)
    try:
        try:
            manifest = json.loads(_read_regular_at(descriptor, MANIFEST_NAME))
            checksum_document = json.loads(_read_regular_at(descriptor, CHECKSUM_NAME))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupError(f"archive metadata is unreadable: {exc}") from exc
        actual_hashes = _walk_archive_directory(descriptor, "", False, False)
        _assert_directory_identity(path, identity)
    finally:
        os.close(descriptor)
    if manifest.get("org") != org or manifest.get("bucket") != bucket:
        raise BackupError("explicit org/bucket do not match the archive manifest")
    if manifest.get("format_version") != 2:
        raise BackupError("archive manifest is incomplete or unsupported")
    manifest_allows_empty = manifest.get("empty_snapshot_allowed") is True
    if allow_empty_snapshot and not manifest_allows_empty:
        raise BackupError("archive was not created with an empty-snapshot override")
    validate_verification_summary(
        manifest.get("verification_summary"),
        allow_empty_snapshot=allow_empty_snapshot and manifest_allows_empty,
    )
    expected = checksum_document.get("sha256")
    if not isinstance(expected, dict) or not expected:
        raise BackupError("archive checksum document is empty or invalid")
    if set(expected) != set(actual_hashes):
        raise BackupError("archive file set does not match the checksum document")
    mismatches = [
        name for name, digest in actual_hashes.items() if expected[name] != digest
    ]
    if mismatches:
        raise BackupError(f"archive checksum mismatch: {', '.join(mismatches)}")
    return manifest


def assert_bucket_absent(
    influx_bin: str,
    host: str,
    org: str,
    bucket: str,
    token: str,
    command_timeout: float,
) -> None:
    output = run_influx(
        influx_bin,
        # Do not use ``--name`` here.  Influx CLI 2.x returns HTTP 404 / exit 1
        # when that filtered name is absent, which is exactly the safe state a
        # restore needs to prove.  Enumerate the explicit org successfully and
        # perform an exact local name comparison instead.
        [
            "bucket",
            "list",
            "--host",
            host,
            "--org",
            org,
            # Make the successful enumeration explicitly complete.  Influx
            # CLI documents zero as "return all" for this option.
            "--limit",
            "0",
            "--json",
        ],
        token,
        command_timeout,
    )
    if not output.strip():
        raise BackupError("empty bucket-list response; refusing restore")
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise BackupError("could not determine whether restore bucket exists") from exc
    if isinstance(payload, dict):
        if "buckets" in payload:
            payload = payload["buckets"]
        elif "results" in payload:
            payload = payload["results"]
        else:
            raise BackupError("unrecognized bucket-list response; refusing restore")
    if not isinstance(payload, list):
        raise BackupError("unrecognized bucket-list response; refusing restore")
    bucket_names = []
    for entry in payload:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise BackupError("unrecognized bucket-list response; refusing restore")
        bucket_names.append(entry["name"])
    if bucket in bucket_names:
        raise BackupError(f"refusing to restore over existing bucket: {org}/{bucket}")


def retained_staging_evidence(
    label: str,
    path: Path,
    identity: Optional[Tuple[int, int]],
) -> str:
    if identity is None:
        identity_detail = "identity unavailable; do not clean by path"
    else:
        identity_detail = f"device={identity[0]}, inode={identity[1]}"
    return (
        f"{label} retained without automatic cleanup: {path} "
        f"({identity_detail})"
    )


def create_private_staging(
    destination: Path,
) -> Tuple[Path, Tuple[int, int], Tuple[int, int]]:
    parent_identity = validate_secure_archive_parent(destination.parent)
    staging = Path(
        tempfile.mkdtemp(
            dir=str(destination.parent), prefix=f".{destination.name}.staging-"
        )
    )
    staging_identity: Optional[Tuple[int, int]] = None
    try:
        staging_stat = os.lstat(staging)
        staging_identity = _identity(staging_stat)
        if not stat.S_ISDIR(staging_stat.st_mode):
            raise BackupError(f"backup staging path is not a directory: {staging}")
        descriptor, opened_identity = _open_directory_no_follow(
            staging, staging_identity
        )
        try:
            os.fchmod(descriptor, 0o700)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if opened_identity != staging_identity:
            raise BackupError("backup staging identity changed during creation")
        if validate_secure_archive_parent(destination.parent) != parent_identity:
            raise BackupError("backup parent changed during staging creation")
        return staging, staging_identity, parent_identity
    except Exception as exc:
        raise BackupError(
            f"{exc}; "
            + retained_staging_evidence(
                "private backup staging", staging, staging_identity
            )
        ) from exc


def create_private_restore_copy(
    source: Path,
    source_identity: Tuple[int, int],
) -> Tuple[Path, Tuple[int, int], Path, Tuple[int, int]]:
    """Copy a verified archive into a private, read-only restore input."""
    parent_identity = validate_secure_archive_parent(source.parent)
    staging = Path(
        tempfile.mkdtemp(
            dir=str(source.parent), prefix=f".{source.name}.restore-staging-"
        )
    )
    source_descriptor = None
    staging_descriptor = None
    destination_descriptor = None
    staging_identity: Optional[Tuple[int, int]] = None
    try:
        staging_stat = os.lstat(staging)
        staging_identity = _identity(staging_stat)
        if not stat.S_ISDIR(staging_stat.st_mode):
            raise BackupError(f"restore staging path is not a directory: {staging}")
        source_descriptor, opened_source_identity = _open_directory_no_follow(
            source, source_identity
        )
        staging_descriptor, opened_staging_identity = _open_directory_no_follow(
            staging, staging_identity
        )
        if opened_source_identity != source_identity:
            raise BackupError("restore source identity changed before private copy")
        if opened_staging_identity != staging_identity:
            raise BackupError("restore staging identity changed during creation")
        os.fchmod(staging_descriptor, 0o700)
        os.mkdir("archive", 0o700, dir_fd=staging_descriptor)
        destination_descriptor = os.open(
            "archive", _directory_open_flags(), dir_fd=staging_descriptor
        )
        destination_stat = os.fstat(destination_descriptor)
        destination_identity = _identity(destination_stat)
        if not stat.S_ISDIR(destination_stat.st_mode):
            raise BackupError("private restore input is not a directory")
        _copy_archive_directory(
            source_descriptor, destination_descriptor, ""
        )
        os.fchmod(destination_descriptor, 0o500)
        os.fsync(destination_descriptor)
        os.fsync(staging_descriptor)
        _assert_directory_identity(source, source_identity)
        _assert_directory_identity(staging, staging_identity)
        if validate_secure_archive_parent(source.parent) != parent_identity:
            raise BackupError("restore source parent changed during private copy")
    except Exception as exc:
        raise BackupError(
            f"{exc}; "
            + retained_staging_evidence(
                "private restore staging", staging, staging_identity
            )
        ) from exc
    finally:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
        if staging_descriptor is not None:
            os.close(staging_descriptor)
        if source_descriptor is not None:
            os.close(source_descriptor)

    destination = staging / "archive"
    _assert_directory_identity(destination, destination_identity)
    return staging, staging_identity, destination, destination_identity


def _rename_directory_no_replace(source: Path, destination: Path) -> None:
    """Use the platform's atomic no-replace directory rename primitive."""
    library = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if sys.platform == "darwin":
        rename_function = library.renamex_np
        rename_function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename_function.restype = ctypes.c_int
        result = rename_function(source_bytes, destination_bytes, 0x00000004)
    elif sys.platform.startswith("linux"):
        try:
            rename_function = library.renameat2
        except AttributeError as exc:
            raise BackupError(
                "atomic no-replace directory publication is unavailable"
            ) from exc
        rename_function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        rename_function.restype = ctypes.c_int
        result = rename_function(-100, source_bytes, -100, destination_bytes, 1)
    else:
        raise BackupError(
            "atomic no-replace directory publication is unsupported on this platform"
        )
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
            raise BackupError(
                f"refusing to overwrite backup path created concurrently: {destination}"
            )
        raise BackupError(
            f"could not atomically publish backup directory: "
            f"{os.strerror(error_number)}"
        )


def publish_archive_without_overwrite(
    staging_archive: Path,
    destination: Path,
    archive_identity: Tuple[int, int],
    parent_identity: Tuple[int, int],
) -> None:
    _assert_directory_identity(staging_archive, archive_identity)
    if validate_secure_archive_parent(destination.parent) != parent_identity:
        raise BackupError("backup parent changed before archive publication")
    _rename_directory_no_replace(staging_archive, destination)
    _assert_directory_identity(destination, archive_identity)
    parent_descriptor, opened_parent_identity = _open_directory_no_follow(
        destination.parent, parent_identity
    )
    try:
        if opened_parent_identity != parent_identity:
            raise BackupError("backup parent changed during archive publication")
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


def run_backup(args: argparse.Namespace, token: str) -> Mapping[str, object]:
    path = validate_new_archive_path(args.path)
    before = collect_verification_summary(
        args.influx_bin,
        args.host,
        args.org,
        args.bucket,
        token,
        args.command_timeout,
    )
    validate_verification_summary(before, args.allow_empty_snapshot)
    staging, staging_identity, parent_identity = create_private_staging(path)
    staging_archive = staging / "archive"
    try:
        _assert_directory_identity(staging, staging_identity)
        run_influx(
            args.influx_bin,
            [
                "backup",
                "--host",
                args.host,
                "--org",
                args.org,
                "--bucket",
                args.bucket,
                str(staging_archive),
            ],
            token,
            args.command_timeout,
        )
        _assert_directory_identity(staging, staging_identity)
        archive_descriptor, archive_identity = _open_directory_no_follow(
            staging_archive
        )
        os.close(archive_descriptor)
        secure_archive_permissions(staging_archive, archive_identity)
        after = collect_verification_summary(
            args.influx_bin,
            args.host,
            args.org,
            args.bucket,
            token,
            args.command_timeout,
        )
        validate_verification_summary(after, args.allow_empty_snapshot)
        if before != after:
            raise BackupError(
                "source structure/range summary changed during backup; retain the "
                "unpublished staging archive for review, quiesce writers, and retry"
            )
        manifest = {
            "format_version": 2,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "host": args.host,
            "org": args.org,
            "bucket": args.bucket,
            "empty_snapshot_allowed": args.allow_empty_snapshot,
            "source_structure_range_stable": True,
            "verification_summary": after,
        }
        write_archive_json(
            staging_archive, MANIFEST_NAME, manifest, archive_identity
        )
        write_checksums(staging_archive, archive_identity)
        secure_archive_permissions(
            staging_archive, archive_identity, sync_files=True
        )
        verify_archive(
            staging_archive,
            args.org,
            args.bucket,
            allow_empty_snapshot=args.allow_empty_snapshot,
            expected_identity=archive_identity,
        )
        checksum_digest = _sha256(staging_archive / CHECKSUM_NAME)
        publish_archive_without_overwrite(
            staging_archive,
            path,
            archive_identity,
            parent_identity,
        )
        result = {
            "operation": "backup",
            "path": str(path),
            "org": args.org,
            "bucket": args.bucket,
            "source_structure_range_stable": True,
            "archive_checksums": "ok",
            "checksum_document_sha256": checksum_digest,
            "retained_staging_path": str(staging),
            "verification_summary": after,
        }
    except Exception as exc:
        raise BackupError(
            f"{exc}; "
            + retained_staging_evidence(
                "private backup staging", staging, staging_identity
            )
        ) from exc
    return result


def run_restore(args: argparse.Namespace, token: str) -> Mapping[str, object]:
    path = validate_archive_path(args.path)
    archive_descriptor, archive_identity = _open_directory_no_follow(path)
    os.close(archive_descriptor)
    manifest = verify_archive(
        path,
        args.org,
        args.bucket,
        allow_empty_snapshot=args.allow_empty_snapshot,
        expected_identity=archive_identity,
    )
    (
        restore_staging,
        restore_staging_identity,
        restore_input,
        restore_input_identity,
    ) = create_private_restore_copy(path, archive_identity)
    try:
        copied_manifest = verify_archive(
            restore_input,
            args.org,
            args.bucket,
            allow_empty_snapshot=args.allow_empty_snapshot,
            expected_identity=restore_input_identity,
        )
        if copied_manifest != manifest:
            raise BackupError("private restore copy manifest does not match source archive")
        if args.restore_bucket == args.bucket:
            raise BackupError("restore bucket must be new and different from source bucket")
        assert_bucket_absent(
            args.influx_bin,
            args.host,
            args.org,
            args.restore_bucket,
            token,
            args.command_timeout,
        )
        # The network check creates a mutation window.  Re-read every file and
        # checksum from the private copy immediately before invoking restore.
        verify_archive(
            restore_input,
            args.org,
            args.bucket,
            allow_empty_snapshot=args.allow_empty_snapshot,
            expected_identity=restore_input_identity,
        )
        run_influx(
            args.influx_bin,
            [
                "restore",
                "--host",
                args.host,
                "--org",
                args.org,
                "--bucket",
                args.bucket,
                "--new-bucket",
                args.restore_bucket,
                str(restore_input),
            ],
            token,
            args.command_timeout,
        )
        verify_archive(
            restore_input,
            args.org,
            args.bucket,
            allow_empty_snapshot=args.allow_empty_snapshot,
            expected_identity=restore_input_identity,
        )
        restored = collect_verification_summary(
            args.influx_bin,
            args.host,
            args.org,
            args.restore_bucket,
            token,
            args.command_timeout,
        )
        validate_verification_summary(restored, args.allow_empty_snapshot)
        if restored != manifest.get("verification_summary"):
            raise BackupError(
                "restore completed but the measurement/field structure, field-value "
                "counts, or observed first/last ranges do not match"
            )
        checksum_document_sha256 = _sha256(restore_input / CHECKSUM_NAME)
        result = {
            "operation": "restore",
            "path": str(path),
            "org": args.org,
            "source_bucket": args.bucket,
            "restore_bucket": args.restore_bucket,
            "archive_checksums": "ok",
            "checksum_document_sha256": checksum_document_sha256,
            "retained_restore_staging_path": str(restore_staging),
            "structure_and_range_match": True,
            "verification_summary": restored,
        }
    except Exception as exc:
        raise BackupError(
            f"{exc}; "
            + retained_staging_evidence(
                "private restore staging",
                restore_staging,
                restore_staging_identity,
            )
        ) from exc
    return result


def add_scope_arguments(parser: argparse.ArgumentParser, require_token: bool = True) -> None:
    parser.add_argument("--host", required=True, help="explicit InfluxDB URL")
    parser.add_argument("--org", required=True, help="explicit organization")
    parser.add_argument("--bucket", required=True, help="explicit source bucket")
    parser.add_argument("--path", required=True, help="explicit backup directory")
    if require_token:
        parser.add_argument(
            "--token-file",
            required=True,
            help="0400/0600 file containing only the InfluxDB token",
        )
        parser.add_argument(
            "--command-timeout",
            type=float,
            default=3600.0,
            help="finite timeout in seconds for each Influx CLI command (default: 3600)",
        )
    parser.add_argument(
        "--allow-empty-snapshot",
        action="store_true",
        help="permit an audited empty bucket/summary",
    )
    parser.add_argument("--influx-bin", default="influx", help=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verified, non-destructive InfluxDB 2.x backup and restore"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    backup_parser = commands.add_parser("backup", help="create and verify a new archive")
    add_scope_arguments(backup_parser)
    restore_parser = commands.add_parser(
        "restore", help="restore into a brand-new bucket and verify it"
    )
    add_scope_arguments(restore_parser)
    restore_parser.add_argument(
        "--restore-bucket", required=True, help="new bucket; must not already exist"
    )
    verify_parser = commands.add_parser(
        "verify-archive", help="verify archive scope and SHA-256 checksums"
    )
    add_scope_arguments(verify_parser, require_token=False)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.host = validate_host(args.host)
        args.org = validate_name(args.org, "--org")
        args.bucket = validate_name(args.bucket, "--bucket")
        if hasattr(args, "command_timeout") and (
            not math.isfinite(args.command_timeout) or args.command_timeout <= 0
        ):
            raise BackupError("--command-timeout must be finite and greater than zero")
        if args.command == "verify-archive":
            path = validate_archive_path(args.path)
            manifest = verify_archive(
                path,
                args.org,
                args.bucket,
                allow_empty_snapshot=args.allow_empty_snapshot,
            )
            result: Mapping[str, object] = {
                "operation": "verify-archive",
                "path": str(path),
                "org": args.org,
                "bucket": args.bucket,
                "archive_checksums": "ok",
                "checksum_document_sha256": _sha256(path / CHECKSUM_NAME),
                "verification_summary": manifest.get("verification_summary"),
            }
        else:
            _, token = validate_token_file(args.token_file)
            if args.command == "backup":
                result = run_backup(args, token)
            else:
                args.restore_bucket = validate_name(
                    args.restore_bucket, "--restore-bucket"
                )
                result = run_restore(args, token)
    except (BackupError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
