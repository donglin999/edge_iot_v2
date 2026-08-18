from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "sqlite_backup.py"
SPEC = importlib.util.spec_from_file_location("edge_iot_sqlite_backup", SCRIPT)
assert SPEC and SPEC.loader
sqlite_backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sqlite_backup)


class SQLiteBackupCLITests(unittest.TestCase):
    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_wal_backup_and_restore_are_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "live.sqlite3"
            backup = root / "snapshot.sqlite3"
            restored = root / "restored.sqlite3"

            # Keep the writer connection open with auto-checkpoint disabled so
            # the committed rows remain in the WAL while the child process
            # takes its snapshot.
            writer = sqlite3.connect(source)
            try:
                self.assertEqual(writer.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute(
                    "CREATE TABLE configuration_device (id INTEGER PRIMARY KEY, code TEXT)"
                )
                writer.executemany(
                    "INSERT INTO configuration_device(code) VALUES (?)",
                    [("device-a",), ("device-b",)],
                )
                writer.commit()
                self.assertTrue(Path(f"{source}-wal").exists())

                backed_up = self.run_cli(
                    "backup",
                    "--source",
                    str(source),
                    "--destination",
                    str(backup),
                    "--table",
                    "configuration_device",
                )
            finally:
                writer.close()

            self.assertEqual(backed_up.returncode, 0, backed_up.stderr)
            backup_report = json.loads(backed_up.stdout)
            self.assertEqual(backup_report["integrity_check"], "ok")
            self.assertEqual(
                backup_report["key_table_counts"]["configuration_device"], 2
            )
            self.assertTrue(backup_report["destination_sha256"])
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            backup_temporary = Path(backup_report["retained_temporary_path"])
            self.assertTrue(backup_temporary.is_file())
            self.assertEqual(backup_temporary.read_bytes(), backup.read_bytes())
            self.assertEqual(
                backup_report["retained_temporary_identity"]["inode"],
                backup_temporary.stat().st_ino,
            )

            restored_result = self.run_cli(
                "restore",
                "--source",
                str(backup),
                "--destination",
                str(restored),
                "--table",
                "configuration_device",
            )
            self.assertEqual(restored_result.returncode, 0, restored_result.stderr)
            restore_report = json.loads(restored_result.stdout)
            self.assertTrue(restore_report["counts_match_source"])
            self.assertEqual(restore_report["source_integrity_check"], "ok")
            restore_temporary = Path(restore_report["retained_temporary_path"])
            self.assertTrue(restore_temporary.is_file())
            self.assertEqual(restore_temporary.read_bytes(), restored.read_bytes())
            with sqlite3.connect(restored) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM configuration_device"
                    ).fetchone()[0],
                    2,
                )

    def test_existing_destination_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sqlite3"
            destination = root / "existing.sqlite3"
            with sqlite3.connect(source) as connection:
                connection.execute("CREATE TABLE configuration_site (id INTEGER)")
                connection.execute("INSERT INTO configuration_site VALUES (1)")
            destination.write_bytes(b"do-not-touch")

            result = self.run_cli(
                "backup",
                "--source",
                str(source),
                "--destination",
                str(destination),
                "--table",
                "configuration_site",
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("refusing to overwrite", result.stderr)
            self.assertEqual(destination.read_bytes(), b"do-not-touch")

    def test_broad_destination_and_unsafe_table_name_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.sqlite3"
            with sqlite3.connect(source) as connection:
                connection.execute("CREATE TABLE example (id INTEGER)")

            broad = self.run_cli(
                "backup",
                "--source",
                str(source),
                "--destination",
                "/unsafe.sqlite3",
                "--table",
                "example",
            )
            self.assertEqual(broad.returncode, 2)
            self.assertIn("broad destination", broad.stderr)

            destination = Path(directory) / "new.sqlite3"
            unsafe_table = self.run_cli(
                "backup",
                "--source",
                str(source),
                "--destination",
                str(destination),
                "--table",
                "example; DROP TABLE example",
            )
            self.assertEqual(unsafe_table.returncode, 2)
            self.assertIn("invalid table", unsafe_table.stderr)
            self.assertFalse(destination.exists())

            unsafe_parent = Path(directory) / "group-writable"
            unsafe_parent.mkdir()
            os.chmod(unsafe_parent, 0o770)
            unsafe_destination = unsafe_parent / "snapshot.sqlite3"
            unsafe_parent_result = self.run_cli(
                "backup",
                "--source",
                str(source),
                "--destination",
                str(unsafe_destination),
                "--table",
                "example",
                "--allow-empty-key-tables",
            )
            self.assertEqual(unsafe_parent_result.returncode, 2)
            self.assertIn("group/world writable", unsafe_parent_result.stderr)
            self.assertFalse(unsafe_destination.exists())

    def test_corrupt_source_is_not_restored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "corrupt.sqlite3"
            destination = root / "restored.sqlite3"
            source.write_bytes(b"this is not sqlite")

            result = self.run_cli(
                "restore",
                "--source",
                str(source),
                "--destination",
                str(destination),
                "--table",
                "configuration_site",
            )

            self.assertEqual(result.returncode, 2)
            self.assertFalse(destination.exists())

    def test_missing_key_tables_require_explicit_legacy_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "legacy.sqlite3"
            destination = root / "strict.sqlite3"
            allowed_destination = root / "allowed.sqlite3"
            with sqlite3.connect(source) as connection:
                connection.execute("CREATE TABLE configuration_site (id INTEGER)")
                connection.execute("INSERT INTO configuration_site VALUES (1)")

            strict = self.run_cli(
                "backup",
                "--source",
                str(source),
                "--destination",
                str(destination),
            )
            self.assertEqual(strict.returncode, 2)
            self.assertIn("required key tables are missing", strict.stderr)
            self.assertFalse(destination.exists())
            self.assertEqual(len(list(root.glob(f".{destination.name}.*.tmp"))), 1)

            allowed = self.run_cli(
                "backup",
                "--source",
                str(source),
                "--destination",
                str(allowed_destination),
                "--allow-missing-key-tables",
            )
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            report = json.loads(allowed.stdout)
            self.assertIn("configuration_device", report["missing_key_tables"])

    def test_publish_race_never_removes_or_overwrites_competitor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sqlite3"
            destination = root / "raced.sqlite3"
            with sqlite3.connect(source) as connection:
                connection.execute("CREATE TABLE configuration_site (id INTEGER)")
                connection.execute("INSERT INTO configuration_site VALUES (1)")

            arguments = argparse.Namespace(
                source=str(source),
                destination=str(destination),
                table=["configuration_site"],
                timeout=30.0,
                allow_missing_key_tables=False,
                allow_empty_key_tables=False,
            )

            def competing_publish(source_descriptor, parent_descriptor, name):
                del source_descriptor
                descriptor = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=parent_descriptor,
                )
                try:
                    os.write(descriptor, b"competitor-owned")
                finally:
                    os.close(descriptor)
                raise FileExistsError(name)

            with mock.patch.object(
                sqlite_backup,
                "_platform_publish_fd",
                side_effect=competing_publish,
            ), mock.patch.object(
                Path,
                "unlink",
                side_effect=AssertionError("SQLite temporaries must never auto-unlink"),
            ):
                with self.assertRaises(sqlite_backup.BackupError):
                    sqlite_backup.run_backup(arguments)

            self.assertEqual(destination.read_bytes(), b"competitor-owned")
            self.assertEqual(len(list(root.glob(f".{destination.name}.*.tmp"))), 1)

            # A same-user rename between parent validation and mkstemp return
            # must be detected.  The tool must not publish through the new
            # directory or remove anything at the replacement pathname.
            stable_parent = root / "stable-parent"
            moved_parent = root / "moved-parent"
            stable_parent.mkdir(mode=0o700)
            replaced_destination = stable_parent / "replaced.sqlite3"
            replacement_arguments = argparse.Namespace(
                source=str(source),
                destination=str(replaced_destination),
                table=["configuration_site"],
                timeout=30.0,
                allow_missing_key_tables=False,
                allow_empty_key_tables=False,
            )
            real_mkstemp = sqlite_backup.tempfile.mkstemp

            def replace_parent_after_create(*args, **kwargs):
                descriptor, temporary_path = real_mkstemp(*args, **kwargs)
                stable_parent.rename(moved_parent)
                stable_parent.mkdir(mode=0o700)
                (stable_parent / "competitor-marker").write_bytes(b"keep")
                return descriptor, temporary_path

            with mock.patch.object(
                sqlite_backup.tempfile,
                "mkstemp",
                side_effect=replace_parent_after_create,
            ):
                with self.assertRaisesRegex(
                    sqlite_backup.BackupError, "destination parent changed"
                ):
                    sqlite_backup.run_backup(replacement_arguments)

            self.assertFalse(replaced_destination.exists())
            self.assertEqual(
                (stable_parent / "competitor-marker").read_bytes(), b"keep"
            )
            self.assertEqual(
                len(list(moved_parent.glob(f".{replaced_destination.name}.*.tmp"))),
                1,
            )

    def test_connection_replacement_is_rejected_before_any_sql(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sqlite3"
            with sqlite3.connect(source) as connection:
                connection.execute("CREATE TABLE configuration_site (id INTEGER)")
                connection.execute("INSERT INTO configuration_site VALUES (1)")

            real_connect = sqlite3.connect
            for replacement_kind in ("symlink", "regular"):
                with self.subTest(replacement_kind=replacement_kind):
                    case_root = root / replacement_kind
                    case_root.mkdir(mode=0o700)
                    destination = case_root / "snapshot.sqlite3"
                    victim = case_root / "victim.sqlite3"
                    victim_bytes = b"victim-must-not-change"
                    victim.write_bytes(victim_bytes)
                    moved_stage = case_root / "held-original.sqlite3"
                    replaced = False

                    def replace_at_connect(database, *args, **kwargs):
                        nonlocal replaced
                        if (
                            isinstance(database, str)
                            and database.startswith("file:")
                            and "mode=rw" in database
                            and not replaced
                        ):
                            replaced = True
                            temporary = Path(unquote(urlsplit(database).path))
                            temporary.rename(moved_stage)
                            if replacement_kind == "symlink":
                                temporary.symlink_to(victim)
                            else:
                                temporary.write_bytes(victim_bytes)
                        return real_connect(database, *args, **kwargs)

                    arguments = argparse.Namespace(
                        source=str(source),
                        destination=str(destination),
                        table=["configuration_site"],
                        timeout=30.0,
                        allow_missing_key_tables=False,
                        allow_empty_key_tables=False,
                    )
                    with mock.patch.object(
                        sqlite_backup.sqlite3,
                        "connect",
                        side_effect=replace_at_connect,
                    ):
                        with self.assertRaisesRegex(
                            sqlite_backup.BackupError,
                            "did not uniquely bind",
                        ):
                            sqlite_backup.run_backup(arguments)

                    self.assertTrue(replaced)
                    self.assertEqual(victim.read_bytes(), victim_bytes)
                    if replacement_kind == "regular":
                        temporary_names = list(
                            case_root.glob(f".{destination.name}.*.tmp")
                        )
                        self.assertEqual(len(temporary_names), 1)
                        self.assertEqual(temporary_names[0].read_bytes(), victim_bytes)
                    self.assertFalse(destination.exists())
                    self.assertTrue(moved_stage.exists())

    def test_fd_publication_ignores_replaced_temp_name_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sqlite3"
            with sqlite3.connect(source) as connection:
                connection.execute("CREATE TABLE configuration_site (id INTEGER)")
                connection.execute("INSERT INTO configuration_site VALUES (7)")

            def arguments_for(destination):
                return argparse.Namespace(
                    source=str(source),
                    destination=str(destination),
                    table=["configuration_site"],
                    timeout=30.0,
                    allow_missing_key_tables=False,
                    allow_empty_key_tables=False,
                )

            destination = root / "fd-published.sqlite3"
            moved_stage = root / "verified-stage-moved.sqlite3"
            competitor_bytes = b"competitor-at-old-stage-name"

            def copy_held_fd_after_path_replacement(
                source_descriptor, parent_descriptor, name
            ):
                temporary_names = list(
                    root.glob(f".{destination.name}.*.tmp")
                )
                self.assertEqual(len(temporary_names), 1)
                temporary_names[0].rename(moved_stage)
                temporary_names[0].write_bytes(competitor_bytes)
                output = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=parent_descriptor,
                )
                try:
                    offset = 0
                    while True:
                        chunk = os.pread(source_descriptor, 65536, offset)
                        if not chunk:
                            break
                        os.write(output, chunk)
                        offset += len(chunk)
                    os.fsync(output)
                finally:
                    os.close(output)

            with mock.patch.object(
                sqlite_backup,
                "_platform_publish_fd",
                side_effect=copy_held_fd_after_path_replacement,
            ):
                report = sqlite_backup.run_backup(arguments_for(destination))

            self.assertEqual(
                Path(report["retained_temporary_path"]).read_bytes(),
                competitor_bytes,
            )
            self.assertEqual(destination.read_bytes(), moved_stage.read_bytes())
            with sqlite3.connect(destination) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT id FROM configuration_site"
                    ).fetchone()[0],
                    7,
                )

            unsupported = root / "unsupported.sqlite3"
            with mock.patch.object(
                sqlite_backup,
                "_platform_publish_fd",
                side_effect=OSError(95, "not supported"),
            ):
                with self.assertRaisesRegex(
                    sqlite_backup.BackupError, "fd-safe no-replace"
                ):
                    sqlite_backup.run_backup(arguments_for(unsupported))
            self.assertFalse(unsupported.exists())
            self.assertEqual(
                len(list(root.glob(f".{unsupported.name}.*.tmp"))), 1
            )

    def test_empty_key_tables_require_explicit_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "empty.sqlite3"
            strict_destination = root / "strict.sqlite3"
            allowed_destination = root / "allowed.sqlite3"
            with sqlite3.connect(source) as connection:
                connection.execute("CREATE TABLE configuration_site (id INTEGER)")

            strict = self.run_cli(
                "backup",
                "--source",
                str(source),
                "--destination",
                str(strict_destination),
                "--table",
                "configuration_site",
            )
            self.assertEqual(strict.returncode, 2)
            self.assertIn("all selected key tables are empty", strict.stderr)
            self.assertFalse(strict_destination.exists())

            allowed = self.run_cli(
                "backup",
                "--source",
                str(source),
                "--destination",
                str(allowed_destination),
                "--table",
                "configuration_site",
                "--allow-empty-key-tables",
            )
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            self.assertEqual(
                json.loads(allowed.stdout)["key_table_counts"]["configuration_site"],
                0,
            )


if __name__ == "__main__":
    unittest.main()
