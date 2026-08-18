#!/usr/bin/env python3
"""Create and restore verified SQLite snapshots without copying a live WAL file.

The only copy primitive in this module is ``sqlite3.Connection.backup``.  It
produces a transactionally consistent snapshot even when the source database
is in WAL mode.  Destination files are always created exclusively; an
existing file is never replaced.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple


DEFAULT_KEY_TABLES: Tuple[str, ...] = (
    "configuration_site",
    "configuration_scadagateway",
    "configuration_device",
    "configuration_channel",
    "configuration_pointtemplate",
    "configuration_point",
    "configuration_acqtask",
    "configuration_taskpoint",
    "configuration_configversion",
    "configuration_importjob",
    "configuration_workerendpoint",
    "configuration_taskrun",
    "acquisition_acquisitionsession",
    "acquisition_datapoint",
    "acquisition_alarmrule",
    "acquisition_alarm",
)
SAFE_SUFFIXES = {".sqlite3", ".sqlite", ".db", ".backup"}
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class BackupError(RuntimeError):
    """A validation, backup, or verification failure."""


def validate_source(path_value: str) -> Path:
    path = Path(path_value).expanduser()
    if path.is_symlink():
        raise BackupError(f"source must not be a symlink: {path}")
    path = path.resolve(strict=False)
    if not path.exists() or not path.is_file():
        raise BackupError(f"source is not a regular file: {path}")
    return path


def validate_secure_destination_parent(path: Path) -> Tuple[int, int]:
    """Require a stable, owner-controlled directory for temporary databases."""
    try:
        parent_stat = os.lstat(path)
    except OSError as exc:
        raise BackupError(f"could not inspect destination parent {path}: {exc}") from exc
    if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
        raise BackupError(f"destination parent must be a real directory: {path}")
    if parent_stat.st_uid != os.geteuid():
        raise BackupError(f"destination parent must be owned by the current user: {path}")
    if stat.S_IMODE(parent_stat.st_mode) & 0o022:
        raise BackupError(
            f"destination parent must not be group/world writable: {path}"
        )
    return parent_stat.st_dev, parent_stat.st_ino


def validate_new_destination(path_value: str, source: Optional[Path] = None) -> Path:
    raw = Path(path_value).expanduser()
    if raw.is_symlink():
        raise BackupError(f"destination must not be a symlink: {raw}")
    # Keep the final parent component observable instead of resolving a parent
    # symlink away before applying the trust-boundary checks below.
    path = Path(os.path.abspath(raw))
    broad_paths = {Path("/"), Path.home().resolve(), Path.cwd().resolve()}
    if path in broad_paths or path.parent == Path("/"):
        raise BackupError(f"refusing broad destination path: {path}")
    if path.suffix.lower() not in SAFE_SUFFIXES:
        allowed = ", ".join(sorted(SAFE_SUFFIXES))
        raise BackupError(f"destination must be a database file ({allowed}): {path}")
    if path.exists() or path.is_symlink():
        raise BackupError(f"refusing to overwrite existing destination: {path}")
    if not path.parent.exists() or not path.parent.is_dir():
        raise BackupError(f"destination parent must already exist: {path.parent}")
    validate_secure_destination_parent(path.parent)
    if source is not None and path == source.resolve():
        raise BackupError("source and destination must be different files")
    return path


def validate_tables(values: Optional[Iterable[str]]) -> Tuple[str, ...]:
    tables = tuple(values or DEFAULT_KEY_TABLES)
    if not tables:
        raise BackupError("at least one key table is required")
    invalid = [name for name in tables if not IDENTIFIER.fullmatch(name)]
    if invalid:
        raise BackupError(f"invalid table name(s): {', '.join(invalid)}")
    return tables


def connect_read_only(path: Path, timeout: float) -> sqlite3.Connection:
    uri = f"{path.as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=timeout)
    connection.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
    return connection


def inspect_connection(
    connection: sqlite3.Connection, tables: Sequence[str], display_name: object
) -> Mapping[str, object]:
    """Verify a database through an already-bound SQLite connection."""
    integrity_rows = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
    if integrity_rows != ["ok"]:
        details = "; ".join(integrity_rows[:10])
        raise BackupError(f"integrity_check failed for {display_name}: {details}")

    existing = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    counts: Dict[str, Optional[int]] = {}
    for table in tables:
        if table not in existing:
            counts[table] = None
            continue
        # Table names have already passed the strict IDENTIFIER allow-list.
        count = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        counts[table] = int(count)

    return {
        "integrity_check": "ok",
        "key_table_counts": counts,
        "missing_key_tables": [name for name, count in counts.items() if count is None],
    }


def inspect_database(
    path: Path, tables: Sequence[str], timeout: float
) -> Mapping[str, object]:
    connection = connect_read_only(path, timeout)
    try:
        return inspect_connection(connection, tables, path)
    finally:
        connection.close()


def require_complete_key_tables(
    inspection: Mapping[str, object],
    allow_missing_key_tables: bool,
    allow_empty_key_tables: bool,
) -> None:
    missing = inspection["missing_key_tables"]
    if missing and not allow_missing_key_tables:
        raise BackupError(
            "required key tables are missing: "
            + ", ".join(str(table) for table in missing)
            + "; use --allow-missing-key-tables only for an audited legacy schema"
        )
    counts = inspection["key_table_counts"]
    if not isinstance(counts, dict):
        raise BackupError("key-table count report is invalid")
    if not any(isinstance(count, int) and count > 0 for count in counts.values()):
        if not allow_empty_key_tables:
            raise BackupError(
                "all selected key tables are empty; use --allow-empty-key-tables "
                "only for an audited empty configuration"
            )


def _file_identity(file_stat: os.stat_result) -> Tuple[int, int]:
    return file_stat.st_dev, file_stat.st_ino


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _regular_read_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _assert_parent_path_identity(
    parent: Path,
    parent_descriptor: int,
    expected_identity: Tuple[int, int],
) -> None:
    try:
        descriptor_stat = os.fstat(parent_descriptor)
        path_stat = os.lstat(parent)
    except OSError as exc:
        raise BackupError(f"destination parent changed or disappeared: {parent}") from exc
    if (
        not stat.S_ISDIR(descriptor_stat.st_mode)
        or stat.S_ISLNK(path_stat.st_mode)
        or not stat.S_ISDIR(path_stat.st_mode)
        or _file_identity(descriptor_stat) != expected_identity
        or _file_identity(path_stat) != expected_identity
    ):
        raise BackupError("destination parent changed during snapshot creation")


def _assert_stage_name_identity(
    temporary: Path,
    parent_descriptor: int,
    expected_identity: Tuple[int, int],
) -> None:
    try:
        temporary_stat = os.stat(
            temporary.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise BackupError(f"temporary snapshot changed or disappeared: {temporary}") from exc
    if not stat.S_ISREG(temporary_stat.st_mode) or (
        _file_identity(temporary_stat) != expected_identity
    ):
        raise BackupError(f"temporary snapshot identity changed: {temporary}")


class SnapshotStage:
    def __init__(
        self,
        path: Path,
        descriptor: int,
        identity: Tuple[int, int],
        parent_descriptor: int,
        parent_identity: Tuple[int, int],
    ) -> None:
        self.path = path
        self.descriptor = descriptor
        self.identity = identity
        self.parent_descriptor = parent_descriptor
        self.parent_identity = parent_identity

    def close(self) -> None:
        os.close(self.descriptor)
        os.close(self.parent_descriptor)


def allocate_private_temporary(
    destination: Path,
) -> SnapshotStage:
    expected_parent_identity = validate_secure_destination_parent(destination.parent)
    try:
        parent_descriptor = os.open(destination.parent, _directory_open_flags())
    except OSError as exc:
        raise BackupError(
            f"could not securely open destination parent {destination.parent}: {exc}"
        ) from exc
    descriptor: Optional[int] = None
    temporary: Optional[Path] = None
    try:
        _assert_parent_path_identity(
            destination.parent, parent_descriptor, expected_parent_identity
        )
        try:
            os.stat(
                destination.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise BackupError(f"refusing to overwrite existing destination: {destination}")

        descriptor, path_value = tempfile.mkstemp(
            dir=str(destination.parent),
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        temporary = Path(path_value)
        os.fchmod(descriptor, 0o600)
        temporary_identity = _file_identity(os.fstat(descriptor))
        _assert_parent_path_identity(
            destination.parent, parent_descriptor, expected_parent_identity
        )
        _assert_stage_name_identity(
            temporary, parent_descriptor, temporary_identity
        )
    except Exception as exc:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_descriptor)
        retained = temporary if temporary is not None else "not allocated"
        raise BackupError(
            f"{exc}; no automatic cleanup was attempted; private temporary: {retained}"
        ) from exc
    assert descriptor is not None and temporary is not None
    return SnapshotStage(
        path=temporary,
        descriptor=descriptor,
        identity=temporary_identity,
        parent_descriptor=parent_descriptor,
        parent_identity=expected_parent_identity,
    )


def _process_fd_directory() -> Path:
    if sys.platform.startswith("linux") and Path("/proc/self/fd").is_dir():
        return Path("/proc/self/fd")
    if sys.platform == "darwin" and Path("/dev/fd").is_dir():
        return Path("/dev/fd")
    raise BackupError(
        "this platform cannot safely bind SQLite's connection descriptor"
    )


def _open_process_descriptors() -> set:
    descriptor_directory = _process_fd_directory()
    descriptors = set()
    try:
        names = os.listdir(descriptor_directory)
    except OSError as exc:
        raise BackupError("could not enumerate process descriptors") from exc
    for name in names:
        if not name.isdigit():
            continue
        descriptor = int(name)
        try:
            os.fstat(descriptor)
        except OSError:
            # /dev/fd and /proc/self/fd expose their own short-lived scan fd.
            continue
        descriptors.add(descriptor)
    return descriptors


def open_bound_destination(
    stage: SnapshotStage, timeout: float
) -> Tuple[sqlite3.Connection, int]:
    """Open the stage by name, then prove SQLite opened the held inode.

    No SQL or PRAGMA is executed until exactly one newly opened process fd is
    bound to the mkstemp inode.  A same-UID path replacement therefore fails
    before SQLite is allowed to write through the connection.
    """
    _assert_parent_path_identity(
        stage.path.parent, stage.parent_descriptor, stage.parent_identity
    )
    _assert_stage_name_identity(stage.path, stage.parent_descriptor, stage.identity)
    before = _open_process_descriptors()
    connection: Optional[sqlite3.Connection] = None
    try:
        connection = sqlite3.connect(
            f"{stage.path.as_uri()}?mode=rw",
            uri=True,
            timeout=timeout,
        )
        after = _open_process_descriptors()
        matching = []
        for descriptor in sorted(after - before):
            try:
                descriptor_stat = os.fstat(descriptor)
            except OSError:
                continue
            if stat.S_ISREG(descriptor_stat.st_mode) and (
                _file_identity(descriptor_stat) == stage.identity
            ):
                matching.append(descriptor)
        if len(matching) != 1:
            raise BackupError(
                "SQLite connection did not uniquely bind to the held temporary inode"
            )
        sqlite_descriptor = matching[0]
        _assert_parent_path_identity(
            stage.path.parent, stage.parent_descriptor, stage.parent_identity
        )
        # This is deliberately the first statement on the new connection.
        connection.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
        return connection, sqlite_descriptor
    except Exception:
        if connection is not None:
            connection.close()
        raise


def _assert_descriptor_identity(
    descriptor: int, expected_identity: Tuple[int, int], label: str
) -> os.stat_result:
    try:
        descriptor_stat = os.fstat(descriptor)
    except OSError as exc:
        raise BackupError(f"{label} descriptor changed or closed") from exc
    if not stat.S_ISREG(descriptor_stat.st_mode) or (
        _file_identity(descriptor_stat) != expected_identity
    ):
        raise BackupError(f"{label} descriptor identity changed")
    return descriptor_stat


def copy_with_backup_api(
    source: Path,
    destination_connection: sqlite3.Connection,
    sqlite_descriptor: int,
    expected_identity: Tuple[int, int],
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout

    def enforce_deadline(status: int, remaining: int, total: int) -> None:
        del status, remaining, total
        if time.monotonic() > deadline:
            raise BackupError(f"SQLite backup exceeded {timeout:g} seconds")

    source_connection = connect_read_only(source, timeout)
    try:
        source_connection.backup(
            destination_connection,
            pages=256,
            progress=enforce_deadline,
            sleep=0.05,
        )
        # The backup API copies the source's WAL journal-mode flag into the
        # standalone file.  Switch the completed snapshot to DELETE mode so
        # verification/restoration needs no sibling -shm/-wal file.
        journal_mode = destination_connection.execute(
            "PRAGMA journal_mode = DELETE"
        ).fetchone()[0]
        if str(journal_mode).lower() != "delete":
            raise BackupError("could not make the SQLite snapshot standalone")
        destination_connection.commit()
        _assert_descriptor_identity(
            sqlite_descriptor, expected_identity, "SQLite connection"
        )
    finally:
        source_connection.close()


def sha256_descriptor(
    descriptor: int, expected_identity: Tuple[int, int], label: str
) -> str:
    before = _assert_descriptor_identity(descriptor, expected_identity, label)
    digest = hashlib.sha256()
    offset = 0
    while offset < before.st_size:
        chunk = os.pread(descriptor, min(1024 * 1024, before.st_size - offset), offset)
        if not chunk:
            raise BackupError(f"{label} ended while hashing")
        digest.update(chunk)
        offset += len(chunk)
    after = _assert_descriptor_identity(descriptor, expected_identity, label)
    if (
        after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise BackupError(f"{label} changed while hashing")
    return digest.hexdigest()


def _ctypes_function(name: str):
    library = ctypes.CDLL(None, use_errno=True)
    try:
        return getattr(library, name)
    except AttributeError as exc:
        raise OSError(errno.ENOTSUP, f"{name} is unavailable") from exc


def _call_linkat(
    source_descriptor: int,
    source_name: bytes,
    destination_parent_descriptor: int,
    destination_name: bytes,
    flags: int,
) -> None:
    linkat = _ctypes_function("linkat")
    linkat.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    linkat.restype = ctypes.c_int
    if linkat(
        source_descriptor,
        source_name,
        destination_parent_descriptor,
        destination_name,
        flags,
    ) != 0:
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise FileExistsError(error_number, os.strerror(error_number))
        raise OSError(error_number, os.strerror(error_number))


def _linux_publish_fd(
    source_descriptor: int, parent_descriptor: int, destination_name: str
) -> None:
    encoded_name = os.fsencode(destination_name)
    try:
        # Linux anonymous-fd publication; never resolves the temporary pathname.
        _call_linkat(source_descriptor, b"", parent_descriptor, encoded_name, 0x1000)
        return
    except FileExistsError:
        raise
    except OSError as direct_error:
        descriptor_link = f"/proc/self/fd/{source_descriptor}"
        if not Path("/proc/self/fd").is_dir():
            raise direct_error
        try:
            # Safe fallback still dereferences the kernel fd link, not the
            # mutable temporary pathname.  AT_SYMLINK_FOLLOW is required.
            _call_linkat(-100, os.fsencode(descriptor_link), parent_descriptor, encoded_name, 0x400)
            return
        except FileExistsError:
            raise
        except OSError as fallback_error:
            raise OSError(
                fallback_error.errno,
                f"linkat fd publication failed ({direct_error}); fallback failed: {fallback_error}",
            ) from fallback_error


def _darwin_publish_fd(
    source_descriptor: int, parent_descriptor: int, destination_name: str
) -> None:
    fclonefileat = _ctypes_function("fclonefileat")
    fclonefileat.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    fclonefileat.restype = ctypes.c_int
    if fclonefileat(
        source_descriptor, parent_descriptor, os.fsencode(destination_name), 0
    ) != 0:
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise FileExistsError(error_number, os.strerror(error_number))
        raise OSError(error_number, os.strerror(error_number))


def _platform_publish_fd(
    source_descriptor: int, parent_descriptor: int, destination_name: str
) -> None:
    if sys.platform.startswith("linux"):
        _linux_publish_fd(source_descriptor, parent_descriptor, destination_name)
        return
    if sys.platform == "darwin":
        _darwin_publish_fd(source_descriptor, parent_descriptor, destination_name)
        return
    raise OSError(errno.ENOTSUP, "fd-based no-replace publication is unsupported")


def publish_without_overwrite(
    stage: SnapshotStage,
    destination: Path,
    expected_digest: str,
) -> None:
    """Publish only the held verified fd, never the mutable staging pathname."""
    _assert_parent_path_identity(
        destination.parent, stage.parent_descriptor, stage.parent_identity
    )
    if sha256_descriptor(stage.descriptor, stage.identity, "temporary snapshot") != expected_digest:
        raise BackupError("temporary snapshot digest changed before publication")
    try:
        _platform_publish_fd(
            stage.descriptor, stage.parent_descriptor, destination.name
        )
    except FileExistsError as exc:
        raise BackupError(
            f"refusing to overwrite destination created concurrently: {destination}"
        ) from exc
    except OSError as exc:
        raise BackupError(
            f"could not publish snapshot with a supported fd-safe no-replace primitive: {exc}"
        ) from exc

    final_descriptor: Optional[int] = None
    try:
        final_descriptor = os.open(
            destination.name,
            _regular_read_flags(),
            dir_fd=stage.parent_descriptor,
        )
        final_stat = os.fstat(final_descriptor)
        if not stat.S_ISREG(final_stat.st_mode):
            raise BackupError("published SQLite snapshot is not a regular file")
        final_identity = _file_identity(final_stat)
        final_digest = sha256_descriptor(
            final_descriptor, final_identity, "published snapshot"
        )
        if final_digest != expected_digest:
            raise BackupError("published SQLite snapshot digest does not match staging")
        os.fsync(final_descriptor)
    finally:
        if final_descriptor is not None:
            os.close(final_descriptor)
    os.fsync(stage.parent_descriptor)
    _assert_parent_path_identity(
        destination.parent, stage.parent_descriptor, stage.parent_identity
    )


def build_and_publish_snapshot(
    source: Path,
    destination: Path,
    tables: Sequence[str],
    timeout: float,
    allow_missing_key_tables: bool,
    allow_empty_key_tables: bool,
    expected_counts: Optional[object] = None,
) -> Tuple[Mapping[str, object], str, Path, Tuple[int, int]]:
    stage = allocate_private_temporary(destination)
    destination_connection: Optional[sqlite3.Connection] = None
    try:
        destination_connection, sqlite_descriptor = open_bound_destination(
            stage, timeout
        )
        copy_with_backup_api(
            source,
            destination_connection,
            sqlite_descriptor,
            stage.identity,
            timeout,
        )
        inspection = inspect_connection(
            destination_connection, tables, stage.path
        )
        require_complete_key_tables(
            inspection,
            allow_missing_key_tables,
            allow_empty_key_tables,
        )
        if (
            expected_counts is not None
            and inspection["key_table_counts"] != expected_counts
        ):
            raise BackupError("restored key-table counts do not match the backup")
        _assert_descriptor_identity(
            sqlite_descriptor, stage.identity, "SQLite connection"
        )
        destination_connection.close()
        destination_connection = None
        digest = sha256_descriptor(
            stage.descriptor, stage.identity, "temporary snapshot"
        )
        os.fsync(stage.descriptor)
        publish_without_overwrite(stage, destination, digest)
        return inspection, digest, stage.path, stage.identity
    except Exception as exc:
        raise BackupError(
            f"{exc}; private temporary was retained for manual digest/inode-checked "
            f"cleanup: {stage.path} (device={stage.identity[0]}, inode={stage.identity[1]})"
        ) from exc
    finally:
        if destination_connection is not None:
            destination_connection.close()
        stage.close()


def _report(
    operation: str,
    source: Path,
    destination: Path,
    inspection: Mapping[str, object],
    destination_sha256: str,
    retained_temporary: Path,
    retained_temporary_identity: Tuple[int, int],
) -> Mapping[str, object]:
    return {
        "operation": operation,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": str(source),
        "destination": str(destination),
        "destination_sha256": destination_sha256,
        "retained_temporary_path": str(retained_temporary),
        "retained_temporary_identity": {
            "device": retained_temporary_identity[0],
            "inode": retained_temporary_identity[1],
        },
        **inspection,
    }


def run_backup(args: argparse.Namespace) -> Mapping[str, object]:
    source = validate_source(args.source)
    destination = validate_new_destination(args.destination, source)
    tables = validate_tables(args.table)
    inspection, digest, retained_temporary, retained_identity = build_and_publish_snapshot(
        source,
        destination,
        tables,
        args.timeout,
        args.allow_missing_key_tables,
        args.allow_empty_key_tables,
    )
    return _report(
        "backup",
        source,
        destination,
        inspection,
        digest,
        retained_temporary,
        retained_identity,
    )


def run_restore(args: argparse.Namespace) -> Mapping[str, object]:
    source = validate_source(args.source)
    destination = validate_new_destination(args.destination, source)
    tables = validate_tables(args.table)
    source_inspection = inspect_database(source, tables, args.timeout)
    require_complete_key_tables(
        source_inspection,
        args.allow_missing_key_tables,
        args.allow_empty_key_tables,
    )
    (
        destination_inspection,
        digest,
        retained_temporary,
        retained_identity,
    ) = build_and_publish_snapshot(
        source,
        destination,
        tables,
        args.timeout,
        args.allow_missing_key_tables,
        args.allow_empty_key_tables,
        expected_counts=source_inspection["key_table_counts"],
    )

    report = dict(
        _report(
            "restore",
            source,
            destination,
            destination_inspection,
            digest,
            retained_temporary,
            retained_identity,
        )
    )
    report["source_integrity_check"] = source_inspection["integrity_check"]
    report["counts_match_source"] = True
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verified, no-overwrite SQLite backup and restore"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("backup", "take an online-consistent snapshot with sqlite3 backup API"),
        ("restore", "restore a verified backup into a brand-new database file"),
    ):
        subparser = subparsers.add_parser(command, help=help_text)
        subparser.add_argument("--source", required=True, help="source SQLite file")
        subparser.add_argument(
            "--destination", required=True, help="new destination; must not exist"
        )
        subparser.add_argument(
            "--table",
            action="append",
            help="key table to count (repeatable; defaults to Edge IoT core tables)",
        )
        subparser.add_argument(
            "--timeout", type=float, default=30.0, help="SQLite lock timeout in seconds"
        )
        subparser.add_argument(
            "--allow-missing-key-tables",
            action="store_true",
            help="permit an audited legacy schema with missing key tables",
        )
        subparser.add_argument(
            "--allow-empty-key-tables",
            action="store_true",
            help="permit an audited empty configuration snapshot",
        )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a finite value greater than zero")
    try:
        result = run_backup(args) if args.command == "backup" else run_restore(args)
    except (BackupError, sqlite3.Error, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
