from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "influx_backup.py"
RUNBOOK = SCRIPT.parents[2] / "docs" / "migration" / "m0-recovery-runbook.md"
SPEC = importlib.util.spec_from_file_location("edge_iot_influx_backup", SCRIPT)
assert SPEC and SPEC.loader
influx_backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(influx_backup)


COUNT_CSV = """#datatype,string,long,long,string,string
,result,table,_value,_field,_measurement
,,0,2,temperature,motor
"""
FIRST_CSV = """#datatype,string,long,dateTime:RFC3339,string,string
,result,table,_time,_field,_measurement
,,0,2026-08-17T01:00:00Z,temperature,motor
"""
LAST_CSV = """#datatype,string,long,dateTime:RFC3339,string,string
,result,table,_time,_field,_measurement
,,0,2026-08-17T02:00:00Z,temperature,motor
"""


class FakeInflux:
    def __init__(self, bucket_list: object = None, empty_snapshot: bool = False) -> None:
        self.calls = []
        self.bucket_list = [] if bucket_list is None else bucket_list
        self.empty_snapshot = empty_snapshot

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), dict(kwargs)))
        action = command[1]
        stdout = ""
        if action == "query":
            if self.empty_snapshot:
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            query = command[-1]
            if "count(" in query:
                stdout = COUNT_CSV
            elif "first()" in query:
                stdout = FIRST_CSV
            elif "last()" in query:
                stdout = LAST_CSV
        elif action == "backup":
            path = Path(command[-1])
            path.mkdir()
            (path / "20260817T010000Z.1.tar.gz").write_bytes(b"influx archive")
        elif action == "bucket":
            stdout = json.dumps(self.bucket_list)
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")


class InfluxBackupTests(unittest.TestCase):
    def call_main(self, arguments):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            return_code = influx_backup.main(arguments)
        return return_code, stdout.getvalue(), stderr.getvalue()

    def create_token(self, root: Path, token: str = "unit-test-secret") -> Path:
        token_file = root / "influx.token"
        token_file.write_text(token, encoding="utf-8")
        os.chmod(token_file, 0o600)
        return token_file

    def backup_arguments(self, token_file: Path, path: Path):
        return [
            "backup",
            "--host",
            "http://127.0.0.1:8086",
            "--org",
            "Midea",
            "--bucket",
            "Record",
            "--token-file",
            str(token_file),
            "--path",
            str(path),
            "--influx-bin",
            "fake-influx",
        ]

    def create_archive(self, root: Path, fake: FakeInflux):
        token_file = self.create_token(root)
        archive = root / "influx-backup"
        with mock.patch.object(influx_backup.subprocess, "run", side_effect=fake):
            code, output, error = self.call_main(
                self.backup_arguments(token_file, archive)
            )
        self.assertEqual(code, 0, error)
        return token_file, archive, json.loads(output)

    def test_backup_is_scoped_checksummed_and_does_not_leak_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = FakeInflux()
            token_file, archive, report = self.create_archive(root, fake)

            self.assertEqual(report["archive_checksums"], "ok")
            self.assertEqual(
                report["verification_summary"]["field_value_count"], 2
            )
            self.assertEqual(len(report["checksum_document_sha256"]), 64)
            manifest = (archive / influx_backup.MANIFEST_NAME).read_text(
                encoding="utf-8"
            )
            self.assertNotIn("unit-test-secret", manifest)
            self.assertTrue((archive / influx_backup.CHECKSUM_NAME).is_file())
            self.assertEqual(archive.stat().st_mode & 0o777, 0o700)
            self.assertEqual(
                (archive / influx_backup.MANIFEST_NAME).stat().st_mode & 0o777,
                0o600,
            )
            retained_staging = Path(report["retained_staging_path"])
            self.assertTrue(retained_staging.is_dir())
            self.assertEqual(list(retained_staging.iterdir()), [])

            for command, keyword_arguments in fake.calls:
                self.assertNotIn("unit-test-secret", command)
                self.assertEqual(
                    keyword_arguments["env"]["INFLUX_TOKEN"], "unit-test-secret"
                )
            self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)

    def test_restore_requires_new_bucket_and_verifies_structure_and_range(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = FakeInflux()
            token_file, archive, _ = self.create_archive(root, fake)

            restore_args = [
                "restore",
                "--host",
                "http://127.0.0.1:8086",
                "--org",
                "Midea",
                "--bucket",
                "Record",
                "--restore-bucket",
                "Record_m0_restore",
                "--token-file",
                str(token_file),
                "--path",
                str(archive),
                "--influx-bin",
                "fake-influx",
            ]
            with mock.patch.object(influx_backup.subprocess, "run", side_effect=fake):
                code, output, error = self.call_main(restore_args)

            self.assertEqual(code, 0, error)
            report = json.loads(output)
            self.assertTrue(report["structure_and_range_match"])
            restore_calls = [call for call, _ in fake.calls if call[1] == "restore"]
            self.assertEqual(len(restore_calls), 1)
            self.assertIn("--new-bucket", restore_calls[0])
            self.assertNotIn("--full", restore_calls[0])
            restore_input = Path(restore_calls[0][-1])
            self.assertNotEqual(restore_input, archive)
            self.assertEqual(restore_input.stat().st_mode & 0o777, 0o500)
            self.assertEqual(
                (restore_input / "20260817T010000Z.1.tar.gz").stat().st_mode
                & 0o777,
                0o400,
            )
            retained_restore_staging = Path(
                report["retained_restore_staging_path"]
            )
            self.assertEqual(restore_input.parent, retained_restore_staging)

    def test_restore_report_checksum_failure_retains_precise_staging_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_file, archive, _ = self.create_archive(root, FakeInflux())
            restore_fake = FakeInflux()
            arguments = [
                "restore",
                "--host",
                "http://127.0.0.1:8086",
                "--org",
                "Midea",
                "--bucket",
                "Record",
                "--restore-bucket",
                "Record_m0_restore_report_failure",
                "--token-file",
                str(token_file),
                "--path",
                str(archive),
                "--influx-bin",
                "fake-influx",
            ]

            with mock.patch.object(
                influx_backup.subprocess, "run", side_effect=restore_fake
            ), mock.patch.object(
                influx_backup,
                "_sha256",
                side_effect=influx_backup.BackupError(
                    "deterministic report-stage checksum failure"
                ),
            ):
                code, _, error = self.call_main(arguments)

            self.assertEqual(code, 2)
            self.assertIn("deterministic report-stage checksum failure", error)
            self.assertTrue(
                any(call[0][1] == "restore" for call in restore_fake.calls)
            )
            retained = list(root.glob(".influx-backup.restore-staging-*"))
            self.assertEqual(len(retained), 1)
            retained_stat = retained[0].stat()
            self.assertIn(str(retained[0]), error)
            self.assertIn(f"device={retained_stat.st_dev}", error)
            self.assertIn(f"inode={retained_stat.st_ino}", error)

    def test_backup_and_restore_copy_failures_report_staging_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            unavailable_root = root / "identity-unavailable"
            unavailable_root.mkdir(mode=0o700)
            unavailable_destination = unavailable_root / "archive"
            real_lstat = os.lstat

            def fail_first_staging_lstat(path):
                if Path(path).name.startswith(".archive.staging-"):
                    raise OSError("deterministic staging lstat failure")
                return real_lstat(path)

            with mock.patch.object(
                influx_backup.os,
                "lstat",
                side_effect=fail_first_staging_lstat,
            ):
                with self.assertRaises(influx_backup.BackupError) as unavailable:
                    influx_backup.create_private_staging(unavailable_destination)
            self.assertIn(
                "identity unavailable; do not clean by path",
                str(unavailable.exception),
            )
            unavailable_staging = list(
                unavailable_root.glob(".archive.staging-*")
            )
            self.assertEqual(len(unavailable_staging), 1)
            self.assertIn(
                str(unavailable_staging[0]), str(unavailable.exception)
            )

            backup_root = root / "backup-time"
            backup_root.mkdir(mode=0o700)
            backup_token = self.create_token(backup_root)
            backup_path = backup_root / "archive"
            backup_fake = FakeInflux()

            def fail_archive_cli(command, **kwargs):
                if command[1] == "backup":
                    return subprocess.CompletedProcess(
                        command, 1, stdout="", stderr="forced backup failure"
                    )
                return backup_fake(command, **kwargs)

            with mock.patch.object(
                influx_backup.subprocess,
                "run",
                side_effect=fail_archive_cli,
            ):
                code, _, backup_error = self.call_main(
                    self.backup_arguments(backup_token, backup_path)
                )
            self.assertEqual(code, 2)
            backup_staging = list(backup_root.glob(".archive.staging-*"))
            self.assertEqual(len(backup_staging), 1)
            backup_stat = backup_staging[0].stat()
            self.assertIn(str(backup_staging[0]), backup_error)
            self.assertIn(f"device={backup_stat.st_dev}", backup_error)
            self.assertIn(f"inode={backup_stat.st_ino}", backup_error)

            restore_root = root / "restore-copy-time"
            restore_root.mkdir(mode=0o700)
            restore_token, restore_archive, _ = self.create_archive(
                restore_root, FakeInflux()
            )
            restore_arguments = [
                "restore",
                "--host",
                "http://127.0.0.1:8086",
                "--org",
                "Midea",
                "--bucket",
                "Record",
                "--restore-bucket",
                "Record_copy_failure",
                "--token-file",
                str(restore_token),
                "--path",
                str(restore_archive),
                "--influx-bin",
                "fake-influx",
            ]
            with mock.patch.object(
                influx_backup,
                "_copy_archive_directory",
                side_effect=influx_backup.BackupError(
                    "deterministic restore copy failure"
                ),
            ):
                code, _, restore_error = self.call_main(restore_arguments)
            self.assertEqual(code, 2)
            restore_staging = list(
                restore_root.glob(".influx-backup.restore-staging-*")
            )
            self.assertEqual(len(restore_staging), 1)
            restore_stat = restore_staging[0].stat()
            self.assertIn(str(restore_staging[0]), restore_error)
            self.assertIn(f"device={restore_stat.st_dev}", restore_error)
            self.assertIn(f"inode={restore_stat.st_ino}", restore_error)

    def test_restore_refuses_existing_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_fake = FakeInflux()
            token_file, archive, _ = self.create_archive(root, create_fake)
            restore_fake = FakeInflux(bucket_list=[{"name": "Record_m0_restore"}])
            arguments = [
                "restore",
                "--host",
                "http://127.0.0.1:8086",
                "--org",
                "Midea",
                "--bucket",
                "Record",
                "--restore-bucket",
                "Record_m0_restore",
                "--token-file",
                str(token_file),
                "--path",
                str(archive),
                "--influx-bin",
                "fake-influx",
            ]
            with mock.patch.object(
                influx_backup.subprocess, "run", side_effect=restore_fake
            ):
                code, _, error = self.call_main(arguments)

            self.assertEqual(code, 2)
            self.assertIn("existing bucket", error)
            retained = list(root.glob(".influx-backup.restore-staging-*"))
            self.assertEqual(len(retained), 1)
            retained_stat = retained[0].stat()
            self.assertIn(str(retained[0]), error)
            self.assertIn(f"device={retained_stat.st_dev}", error)
            self.assertIn(f"inode={retained_stat.st_ino}", error)
            self.assertFalse(
                any(call[0][1] == "restore" for call in restore_fake.calls)
            )

            race_root = root / "restore-path-race"
            race_root.mkdir(mode=0o700)
            race_token, race_archive, _ = self.create_archive(
                race_root, FakeInflux()
            )
            identity_fake = FakeInflux()

            def tamper_private_copy_after_bucket_check(command, **kwargs):
                completed = identity_fake(command, **kwargs)
                if command[1] == "bucket":
                    staging_roots = list(
                        race_root.glob(".influx-backup.restore-staging-*")
                    )
                    self.assertEqual(len(staging_roots), 1)
                    copied_payload = (
                        staging_roots[0]
                        / "archive"
                        / "20260817T010000Z.1.tar.gz"
                    )
                    os.chmod(copied_payload, 0o600)
                    copied_payload.write_bytes(b"in-place tampering")
                return completed

            race_arguments = list(arguments)
            race_arguments[race_arguments.index("--token-file") + 1] = str(
                race_token
            )
            race_arguments[race_arguments.index("--path") + 1] = str(race_archive)
            with mock.patch.object(
                influx_backup.subprocess,
                "run",
                side_effect=tamper_private_copy_after_bucket_check,
            ):
                code, _, error = self.call_main(race_arguments)
            self.assertEqual(code, 2)
            self.assertIn("archive checksum mismatch", error)
            self.assertFalse(
                any(call[0][1] == "restore" for call in identity_fake.calls)
            )

    def test_bucket_absence_enumerates_org_and_matches_exact_name(self) -> None:
        missing = FakeInflux(
            bucket_list=[
                {"name": "Record_m0_restore_old"},
                {"name": "Record"},
            ]
        )
        with mock.patch.object(
            influx_backup.subprocess, "run", side_effect=missing
        ):
            influx_backup.assert_bucket_absent(
                "fake-influx",
                "http://127.0.0.1:8086",
                "Midea",
                "Record_m0_restore",
                "unit-test-secret",
                10.0,
            )
        bucket_calls = [call for call, _ in missing.calls if call[1] == "bucket"]
        self.assertEqual(len(bucket_calls), 1)
        self.assertNotIn("--name", bucket_calls[0])
        self.assertIn("--limit", bucket_calls[0])
        self.assertEqual(
            bucket_calls[0][bucket_calls[0].index("--limit") + 1], "0"
        )
        self.assertEqual(bucket_calls[0][-1], "--json")

        existing = FakeInflux(
            bucket_list=[{"name": "Record_m0_restore"}]
        )
        with mock.patch.object(
            influx_backup.subprocess, "run", side_effect=existing
        ), self.assertRaisesRegex(influx_backup.BackupError, "existing bucket"):
            influx_backup.assert_bucket_absent(
                "fake-influx",
                "http://127.0.0.1:8086",
                "Midea",
                "Record_m0_restore",
                "unit-test-secret",
                10.0,
            )

    def test_bucket_absence_rejects_abnormal_enumeration_response(self) -> None:
        for payload in (
            {"unexpected": []},
            "not-a-list",
            ["not-a-bucket-object"],
            [{"id": "missing-name"}],
            [{"name": 42}],
        ):
            with self.subTest(payload=payload):
                fake = FakeInflux(bucket_list=payload)
                with mock.patch.object(
                    influx_backup.subprocess, "run", side_effect=fake
                ), self.assertRaisesRegex(
                    influx_backup.BackupError, "unrecognized bucket-list response"
                ):
                    influx_backup.assert_bucket_absent(
                        "fake-influx",
                        "http://127.0.0.1:8086",
                        "Midea",
                        "Record_m0_restore",
                        "unit-test-secret",
                        10.0,
                    )

        def fail_unfiltered_enumeration(command, **kwargs):
            self.assertNotIn("--name", command)
            return subprocess.CompletedProcess(
                command, 1, stdout="", stderr="enumeration failed"
            )

        with mock.patch.object(
            influx_backup.subprocess,
            "run",
            side_effect=fail_unfiltered_enumeration,
        ), self.assertRaisesRegex(influx_backup.BackupError, "influx bucket failed"):
            influx_backup.assert_bucket_absent(
                "fake-influx",
                "http://127.0.0.1:8086",
                "Midea",
                "Record_m0_restore",
                "unit-test-secret",
                10.0,
            )

        def return_invalid_json(command, **kwargs):
            self.assertNotIn("--name", command)
            return subprocess.CompletedProcess(
                command, 0, stdout="not-json", stderr=""
            )

        with mock.patch.object(
            influx_backup.subprocess,
            "run",
            side_effect=return_invalid_json,
        ), self.assertRaisesRegex(
            influx_backup.BackupError,
            "could not determine whether restore bucket exists",
        ):
            influx_backup.assert_bucket_absent(
                "fake-influx",
                "http://127.0.0.1:8086",
                "Midea",
                "Record_m0_restore",
                "unit-test-secret",
                10.0,
            )

    def test_bucket_absence_rejects_exit_zero_empty_stdout(self) -> None:
        def return_empty_success(command, **kwargs):
            self.assertNotIn("--name", command)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with mock.patch.object(
            influx_backup.subprocess,
            "run",
            side_effect=return_empty_success,
        ), self.assertRaisesRegex(
            influx_backup.BackupError,
            "empty bucket-list response; refusing restore",
        ):
            influx_backup.assert_bucket_absent(
                "fake-influx",
                "http://127.0.0.1:8086",
                "Midea",
                "Record_m0_restore",
                "unit-test-secret",
                10.0,
            )

    def test_checksum_tampering_and_special_entries_are_detected(self) -> None:
        # Keep this on a local filesystem so mkfifo exercises a real special
        # entry rather than a mocked stat result.
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            _, archive, _ = self.create_archive(root, FakeInflux())
            archive_file = archive / "20260817T010000Z.1.tar.gz"
            archive_file.write_bytes(b"tampered")

            with self.assertRaises(influx_backup.BackupError):
                influx_backup.verify_archive(archive, "Midea", "Record")

            special_root = root / "special-case"
            special_root.mkdir(mode=0o700)
            _, special_archive, _ = self.create_archive(special_root, FakeInflux())

            fifo = special_archive / "unexpected.fifo"
            os.mkfifo(fifo)
            with self.assertRaisesRegex(
                influx_backup.BackupError, "symlink or special entry"
            ):
                influx_backup.verify_archive(special_archive, "Midea", "Record")
            fifo.unlink()

            outside_file = special_root / "outside"
            outside_file.write_bytes(b"outside")
            symlink = special_archive / "unexpected-link"
            symlink.symlink_to(outside_file)
            with self.assertRaisesRegex(
                influx_backup.BackupError, "symlink or special entry"
            ):
                influx_backup.verify_archive(special_archive, "Midea", "Record")

    def test_broad_path_and_host_credentials_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_file = self.create_token(Path(directory))
            arguments = self.backup_arguments(token_file, Path("/unsafe-influx-backup"))
            code, _, error = self.call_main(arguments)
            self.assertEqual(code, 2)
            self.assertIn("broad backup path", error)

            arguments = self.backup_arguments(
                token_file, Path(directory) / "safe-backup"
            )
            host_index = arguments.index("--host") + 1
            arguments[host_index] = "http://operator:secret@127.0.0.1:8086"
            code, _, error = self.call_main(arguments)
            self.assertEqual(code, 2)
            self.assertIn("must not contain credentials", error)

            unsafe_parent = Path(directory) / "group-writable"
            unsafe_parent.mkdir()
            os.chmod(unsafe_parent, 0o770)
            arguments = self.backup_arguments(
                token_file, unsafe_parent / "archive"
            )
            code, _, error = self.call_main(arguments)
            self.assertEqual(code, 2)
            self.assertIn("group/world writable", error)

            # A competitor creates the final name only after the CLI has
            # completed its staging archive.  Atomic publication must preserve
            # that directory and retain our private staging tree for manual
            # inode-checked cleanup.
            race_root = Path(directory) / "publish-race"
            race_root.mkdir(mode=0o700)
            race_token = self.create_token(race_root)
            raced_archive = race_root / "archive"
            fake = FakeInflux()

            def create_competitor_before_publish(command, **kwargs):
                completed = fake(command, **kwargs)
                if command[1] == "backup":
                    raced_archive.mkdir(mode=0o700)
                    (raced_archive / "competitor-marker").write_bytes(b"keep")
                return completed

            with mock.patch.object(
                influx_backup.subprocess,
                "run",
                side_effect=create_competitor_before_publish,
            ):
                code, _, error = self.call_main(
                    self.backup_arguments(race_token, raced_archive)
                )
            self.assertEqual(code, 2)
            self.assertIn("created concurrently", error)
            self.assertEqual(
                (raced_archive / "competitor-marker").read_bytes(), b"keep"
            )
            self.assertEqual(len(list(race_root.glob(".archive.staging-*"))), 1)

            # If the CLI returns a symlink in place of its designated child,
            # the wrapper must reject it before chmod/hash and leave the victim
            # directory untouched.
            symlink_root = Path(directory) / "symlink-race"
            symlink_root.mkdir(mode=0o700)
            symlink_token = self.create_token(symlink_root)
            symlink_archive = symlink_root / "archive"
            victim = symlink_root / "victim"
            victim.mkdir(mode=0o755)
            symlink_fake = FakeInflux()

            def replace_cli_child_with_symlink(command, **kwargs):
                if command[1] == "backup":
                    Path(command[-1]).symlink_to(victim, target_is_directory=True)
                    return subprocess.CompletedProcess(
                        command, 0, stdout="", stderr=""
                    )
                return symlink_fake(command, **kwargs)

            with mock.patch.object(
                influx_backup.subprocess,
                "run",
                side_effect=replace_cli_child_with_symlink,
            ):
                code, _, error = self.call_main(
                    self.backup_arguments(symlink_token, symlink_archive)
                )
            self.assertEqual(code, 2)
            self.assertIn("securely open archive directory", error)
            self.assertEqual(victim.stat().st_mode & 0o777, 0o755)
            self.assertEqual(list(victim.iterdir()), [])
            self.assertFalse(symlink_archive.exists())
            self.assertEqual(
                len(list(symlink_root.glob(".archive.staging-*"))), 1
            )

            # Replacement during staging identity validation must never be
            # recursively removed by the exception path.
            validation_root = Path(directory) / "validation-race"
            validation_root.mkdir(mode=0o700)
            validation_destination = validation_root / "archive"
            moved_staging = validation_root / "owned-staging-moved"
            real_open_directory = influx_backup._open_directory_no_follow
            replacement_path = None

            def replace_staging_during_validation(path, expected_identity=None):
                nonlocal replacement_path
                if replacement_path is None and ".staging-" in path.name:
                    path.rename(moved_staging)
                    path.mkdir(mode=0o700)
                    (path / "competitor-marker").write_bytes(b"keep")
                    replacement_path = path
                return real_open_directory(path, expected_identity)

            with mock.patch.object(
                influx_backup,
                "_open_directory_no_follow",
                side_effect=replace_staging_during_validation,
            ):
                with self.assertRaises(influx_backup.BackupError):
                    influx_backup.create_private_staging(validation_destination)
            self.assertIsNotNone(replacement_path)
            self.assertEqual(
                (replacement_path / "competitor-marker").read_bytes(), b"keep"
            )

            # A marker appearing immediately after publication (the old cleanup
            # call point) must remain because successful runs never auto-rmdir.
            cleanup_root = Path(directory) / "cleanup-race"
            cleanup_root.mkdir(mode=0o700)
            cleanup_token = self.create_token(cleanup_root)
            cleanup_archive = cleanup_root / "archive"
            real_publish = influx_backup.publish_archive_without_overwrite

            def publish_then_add_competitor_marker(*publish_args, **publish_kwargs):
                real_publish(*publish_args, **publish_kwargs)
                staging_root = Path(publish_args[0]).parent
                (staging_root / "competitor-marker").write_bytes(b"keep")

            with mock.patch.object(
                influx_backup.subprocess, "run", side_effect=FakeInflux()
            ), mock.patch.object(
                influx_backup,
                "publish_archive_without_overwrite",
                side_effect=publish_then_add_competitor_marker,
            ):
                code, output, error = self.call_main(
                    self.backup_arguments(cleanup_token, cleanup_archive)
                )
            self.assertEqual(code, 0, error)
            retained_staging = Path(json.loads(output)["retained_staging_path"])
            self.assertEqual(
                (retained_staging / "competitor-marker").read_bytes(), b"keep"
            )

            # Execute the runbook's actual image-archive wrapper locally with
            # a fake Docker subprocess.  Replacing the staging pathname during
            # `docker image save` must not redirect writes to the victim: the
            # held fd remains both the output and publication authority.
            runbook = RUNBOOK.read_text(encoding="utf-8")

            def embedded_python(tag):
                match = re.search(
                    rf"<<'{tag}'\n(.*?)\n{tag}", runbook, flags=re.DOTALL
                )
                self.assertIsNotNone(match)
                return match.group(1)

            image_ids = ["sha256:" + character * 64 for character in "123"]
            archive_program = embedded_python("PY_IMAGE_ARCHIVE")

            def execute_archive(destination, fake_run):
                output = io.StringIO()
                arguments = [
                    "image-archive-wrapper",
                    str(destination),
                    *image_ids,
                    "30",
                ]
                with mock.patch.object(
                    subprocess, "run", side_effect=fake_run
                ), mock.patch.object(
                    sys, "argv", arguments
                ), contextlib.redirect_stdout(output):
                    exec(
                        compile(
                            archive_program,
                            "<PY_IMAGE_ARCHIVE>",
                            "exec",
                        ),
                        {"__name__": "__main__"},
                    )
                return output.getvalue()

            image_root = Path(directory) / "image-tar-held-fd"
            image_root.mkdir(mode=0o700)
            image_tar = image_root / "old-images.tar"
            moved_stage = image_root / "held-stage-moved"
            victim = image_root / "victim"
            victim_bytes = b"victim-must-not-be-truncated"
            tar_bytes = b"fake-docker-image-tar"
            victim.write_bytes(victim_bytes)

            def replace_path_and_write_held_fd(command, **kwargs):
                self.assertEqual(command, ["docker", "image", "save", *image_ids])
                self.assertEqual(kwargs["stderr"], subprocess.PIPE)
                stages = list(image_root.glob(".old-images.tar.*.staging"))
                self.assertEqual(len(stages), 1)
                stages[0].rename(moved_stage)
                stages[0].symlink_to(victim)
                os.write(kwargs["stdout"], tar_bytes)
                return subprocess.CompletedProcess(
                    command, 0, stdout=None, stderr=b"suppressed-success-detail"
                )

            archive_output = execute_archive(
                image_tar, replace_path_and_write_held_fd
            )
            stage_name, stage_dev, stage_ino, stage_digest = (
                archive_output.strip().split("\t")
            )
            self.assertEqual(Path(stage_name).resolve(), victim.resolve())
            self.assertEqual(victim.read_bytes(), victim_bytes)
            self.assertEqual(moved_stage.read_bytes(), tar_bytes)
            self.assertEqual(image_tar.read_bytes(), tar_bytes)
            self.assertEqual(int(stage_dev), moved_stage.stat().st_dev)
            self.assertEqual(int(stage_ino), moved_stage.stat().st_ino)
            self.assertRegex(stage_digest, r"^[0-9a-f]{64}$")
            self.assertEqual(
                (image_root / "old-images.tar.sha256").read_text(),
                f"{stage_digest}  old-images.tar\n",
            )

            competitor_root = Path(directory) / "image-tar-final-race"
            competitor_root.mkdir(mode=0o700)
            competitor_tar = competitor_root / "old-images.tar"

            def create_final_competitor(command, **kwargs):
                os.write(kwargs["stdout"], tar_bytes)
                competitor_tar.write_bytes(b"competitor-owned")
                return subprocess.CompletedProcess(
                    command, 0, stdout=None, stderr=b"suppressed"
                )

            with self.assertRaisesRegex(SystemExit, "拒绝覆盖"):
                execute_archive(competitor_tar, create_final_competitor)
            self.assertEqual(competitor_tar.read_bytes(), b"competitor-owned")
            self.assertEqual(
                len(list(competitor_root.glob(".old-images.tar.*.staging"))),
                1,
            )

            failure_root = Path(directory) / "image-tar-save-failure"
            failure_root.mkdir(mode=0o700)
            failed_tar = failure_root / "old-images.tar"

            def fail_with_sensitive_stderr(command, **kwargs):
                return subprocess.CompletedProcess(
                    command,
                    1,
                    stdout=None,
                    stderr=b"sensitive-daemon-detail",
                )

            with self.assertRaises(SystemExit) as failure:
                execute_archive(failed_tar, fail_with_sensitive_stderr)
            self.assertNotIn("sensitive-daemon-detail", str(failure.exception))
            self.assertFalse(failed_tar.exists())
            self.assertEqual(
                len(list(failure_root.glob(".old-images.tar.*.staging"))),
                1,
            )

            self.assertIn('["docker", "image", "save", *image_ids]', runbook)
            self.assertIn("stdout=descriptor", runbook)
            self.assertIn("stdin=archive_descriptor", runbook)
            self.assertNotIn("docker image tag", runbook)
            self.assertIn('--force-recreate --pull never', runbook)

    def test_cli_error_redacts_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_file = self.create_token(root)

            def fail_with_echoed_token(command, **kwargs):
                return subprocess.CompletedProcess(
                    command,
                    1,
                    stdout="",
                    stderr="authentication failed for unit-test-secret",
                )

            with mock.patch.object(
                influx_backup.subprocess, "run", side_effect=fail_with_echoed_token
            ):
                code, _, error = self.call_main(
                    self.backup_arguments(token_file, root / "safe-backup")
                )

            self.assertEqual(code, 2)
            self.assertNotIn("unit-test-secret", error)
            self.assertIn("<redacted>", error)

    def test_empty_snapshot_requires_explicit_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_file = self.create_token(root)
            strict_archive = root / "strict-empty"
            strict_fake = FakeInflux(empty_snapshot=True)
            with mock.patch.object(
                influx_backup.subprocess, "run", side_effect=strict_fake
            ):
                code, _, error = self.call_main(
                    self.backup_arguments(token_file, strict_archive)
                )
            self.assertEqual(code, 2)
            self.assertIn("empty Influx verification summary", error)
            self.assertFalse(strict_archive.exists())

            allowed_archive = root / "allowed-empty"
            allowed_arguments = self.backup_arguments(token_file, allowed_archive)
            allowed_arguments.append("--allow-empty-snapshot")
            allowed_fake = FakeInflux(empty_snapshot=True)
            with mock.patch.object(
                influx_backup.subprocess, "run", side_effect=allowed_fake
            ):
                code, output, error = self.call_main(allowed_arguments)
            self.assertEqual(code, 0, error)
            report = json.loads(output)
            self.assertEqual(
                report["verification_summary"]["field_value_count"], 0
            )

    def test_command_timeout_is_bounded_and_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_file = self.create_token(root)
            arguments = self.backup_arguments(token_file, root / "timed-out")
            arguments.extend(["--command-timeout", "0.25"])

            def time_out(command, **kwargs):
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])

            with mock.patch.object(
                influx_backup.subprocess, "run", side_effect=time_out
            ):
                code, _, error = self.call_main(arguments)

            self.assertEqual(code, 2)
            self.assertIn("exceeded 0.25 seconds", error)

    def test_token_file_rejects_group_or_world_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token_file = self.create_token(root)
            os.chmod(token_file, 0o640)
            code, _, error = self.call_main(
                self.backup_arguments(token_file, root / "never-created")
            )
            self.assertEqual(code, 2)
            self.assertIn("permissions must be exactly 0400 or 0600", error)

    def test_token_mode_check_rejects_non_readable_or_executable_owner_modes(self) -> None:
        for mode in (0o000, 0o200, 0o700):
            with self.subTest(mode=oct(mode)):
                with self.assertRaises(influx_backup.BackupError):
                    influx_backup.require_private_token_mode(
                        mode, Path("/not-opened/unit-test.token")
                    )
        for mode in (0o400, 0o600):
            with self.subTest(mode=oct(mode)):
                influx_backup.require_private_token_mode(
                    mode, Path("/not-opened/unit-test.token")
                )

    def test_token_content_is_read_from_one_secure_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token_file = self.create_token(Path(directory))
            os.chmod(token_file, 0o400)
            with mock.patch.object(
                Path,
                "read_text",
                side_effect=AssertionError("path must not be reopened for token read"),
            ):
                resolved, token = influx_backup.validate_token_file(str(token_file))
            self.assertEqual(resolved, token_file.absolute())
            self.assertEqual(token, "unit-test-secret")

    def test_invalid_observed_time_range_is_rejected(self) -> None:
        summary = {
            "series": [
                {
                    "measurement": "motor",
                    "field": "temperature",
                    "count": 2,
                    "first": "2026-08-17T02:00:00Z",
                    "last": "2026-08-17T01:00:00Z",
                }
            ],
            "series_count": 1,
            "field_value_count": 2,
        }
        with self.assertRaises(influx_backup.BackupError):
            influx_backup.validate_verification_summary(
                summary, allow_empty_snapshot=False
            )

    def test_nanosecond_rfc3339_ranges_are_supported(self) -> None:
        first = influx_backup.parse_rfc3339_nanoseconds(
            "2026-08-17T01:00:00.000000001Z"
        )
        last = influx_backup.parse_rfc3339_nanoseconds(
            "2026-08-17T01:00:00.000000009+00:00"
        )
        self.assertLess(first, last)


if __name__ == "__main__":
    unittest.main()
