from __future__ import annotations

import hashlib
import importlib.util
import argparse
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


RECOVERY_ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, RECOVERY_ROOT / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


safety = load_module("m0_safety", "safety.py")
sys.modules["safety"] = safety
evidence_io = load_module("m0_evidence_io", "evidence_io.py")
dind_tls = load_module("m0_dind_tls", "dind_tls.py")
archive = load_module("m0_image_archive", "image_archive.py")
smoke = load_module("m0_stack_smoke", "stack_smoke.py")
celery_smoke = load_module("m0_celery_smoke", "celery_smoke.py")
influx_value_check = load_module("m0_influx_value_check", "influx_value_check.py")


class SafetyTests(unittest.TestCase):
    def test_accepts_unique_project(self):
        self.assertEqual(safety.validate_project("m0ci-run-123456"), "m0ci-run-123456")

    def test_rejects_demo_and_malformed_projects(self):
        for value in ("edge_iot_v2", "m0", "m0ci-UPPER-123", "m0ci-demo", "production"):
            with self.subTest(value=value), self.assertRaises(safety.SafetyError):
                safety.validate_project(value)

    def test_only_accepts_immutable_image_ids(self):
        good = "sha256:" + "a" * 64
        self.assertEqual(safety.validate_image_ids([good]), (good,))
        for bad in ("redis:7", "sha256:abc", "", "sha256:" + "G" * 64):
            with self.subTest(value=bad), self.assertRaises(safety.SafetyError):
                safety.validate_image_ids([bad])

    def test_new_evidence_must_be_direct_child_and_contain_project(self):
        project = "m0ci-run-123456"
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            accepted = root / f"evidence-{project}"
            self.assertEqual(
                safety.validate_new_evidence_path(str(accepted), str(root), project),
                accepted,
            )
            with self.assertRaises(safety.SafetyError):
                safety.validate_new_evidence_path(
                    str(root / "nested" / f"evidence-{project}"), str(root), project
                )
            with self.assertRaises(safety.SafetyError):
                safety.validate_new_evidence_path(str(root / "wrong"), str(root), project)

    def test_created_evidence_requires_exact_mode(self):
        with tempfile.TemporaryDirectory() as raw_root:
            path = Path(raw_root) / "evidence"
            path.mkdir(mode=0o700)
            os.chmod(path, 0o700)
            self.assertEqual(safety.validate_created_private_directory(str(path)), path)
            os.chmod(path, 0o750)
            with self.assertRaises(safety.SafetyError):
                safety.validate_created_private_directory(str(path))

    def test_compose_has_isolation_guards(self):
        path = RECOVERY_ROOT / "docker-compose.m0-ci.yml"
        self.assertEqual(safety.validate_compose_source(str(path)), path.resolve())
        with tempfile.TemporaryDirectory() as raw_root:
            bad = Path(raw_root) / "compose.yml"
            bad.write_text(
                "services:\n  x:\n    container_name: django-iot\n"
                "    pull_policy: never\nnetworks:\n  x:\n    driver: bridge\n"
                "    internal: true\n# host_ip: \"127.0.0.1\"\n"
            )
            with self.assertRaises(safety.SafetyError):
                safety.validate_compose_source(str(bad))

            credential = Path(raw_root) / "credential.yml"
            credential.write_text(
                "services:\n  x:\n    image: sha256:"
                + "a" * 64
                + "\n    pull_policy: never\n    environment:\n"
                "      "
                + "SECRET_KEY"
                + ": "
                + "hardcoded-credential\n"
                "    ports:\n      - target: 80\n        published: \"0\"\n"
                "        host_ip: \"127.0.0.1\"\n"
                "networks:\n  x:\n    driver: bridge\n    internal: true\n"
            )
            with self.assertRaisesRegex(safety.SafetyError, "credential literal"):
                safety.validate_compose_source(str(credential))

    def test_static_scan_rejects_broad_cleanup_without_rg_on_path(self):
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            scripts = root / "recovery"
            scripts.mkdir()
            (scripts / "unsafe.sh").write_text(
                "#!/bin/sh\ndocker system prune --force\n", encoding="utf-8"
            )
            empty_path = root / "no-rg-bin"
            empty_path.mkdir()
            environment = os.environ.copy()
            environment["PATH"] = str(empty_path)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(RECOVERY_ROOT / "safety.py"),
                    "recovery-scripts",
                    "--root",
                    str(scripts),
                ],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIn("broad cleanup", completed.stderr)

    def test_static_scan_rejects_unsafe_evidence_and_plaintext_dind(self):
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            script = root / "unsafe.sh"
            cases = (
                ('printf unsafe > "$EVIDENCE_ROOT/report.json"\n', "exclusive"),
                (
                    "docker run -p 127.0.0.1::"
                    + "23"
                    + "75 image --tls="
                    + "false\n",
                    "plaintext",
                ),
                ("docker cp container:/cert.pem client.pem\n", "mutable-path"),
            )
            for source, message in cases:
                with self.subTest(message=message):
                    script.write_text("#!/bin/sh\n" + source, encoding="utf-8")
                    with self.assertRaisesRegex(safety.SafetyError, message):
                        safety.validate_recovery_scripts(str(root))

    def test_recovery_tool_uses_one_pinned_verified_cli(self):
        source_path = RECOVERY_ROOT / "Dockerfile.tool"
        self.assertEqual(
            safety.validate_recovery_tool_dockerfile(str(source_path)),
            source_path.resolve(),
        )
        source = source_path.read_text(encoding="utf-8")
        mutations = {
            "server image with platform": source.replace(
                "FROM python:3.10-slim AS influx-cli",
                "FROM --platform=linux/amd64 influxdb:2.7 AS influx-cli",
                1,
            ),
            "overridable version": source.replace(
                "ARG TARGETARCH", "ARG TARGETARCH\nARG INFLUX_CLI_VERSION=2.7.5", 1
            ),
            "wrong final stage": source.replace(
                "COPY --from=influx-cli", "COPY --from=unverified-cli", 1
            ),
            "wrong checksum": source.replace(safety.INFLUX_CLI_SHA256, "0" * 64, 1),
            "unused verified stage": source.replace(
                "FROM python:3.10-slim\nCOPY --from=influx-cli",
                "FROM python:3.10-slim AS unverified-cli\n"
                "RUN touch /usr/local/bin/influx\n"
                "FROM python:3.10-slim\nCOPY --from=unverified-cli",
                1,
            ),
            "overwrite after verification": source + "\nRUN printf attacker > /usr/local/bin/influx\n",
            "second unverified download": source
            + "\nRUN curl https://example.invalid/influx -o /usr/local/bin/influx\n",
            "ignored checksum failure": source.replace(
                "sha256sum --check --strict -",
                "sha256sum --check --strict - || true",
                1,
            ),
            "alternate escape directive": "# escape=`\n" + source,
        }
        with tempfile.TemporaryDirectory() as raw_root:
            candidate = Path(raw_root) / "Dockerfile.tool"
            for label, mutated in mutations.items():
                with self.subTest(label=label):
                    candidate.write_text(mutated, encoding="utf-8")
                    with self.assertRaises(safety.SafetyError):
                        safety.validate_recovery_tool_dockerfile(str(candidate))

            comment_in_continuation = source.replace(
                "    && apt-get install",
                "# a pure comment inside the continued RUN is ignored by Docker\n"
                "    && apt-get install",
                1,
            )
            candidate.write_text(comment_in_continuation, encoding="utf-8")
            self.assertEqual(
                safety.validate_recovery_tool_dockerfile(str(candidate)),
                candidate.resolve(),
            )


class InfluxKnownValueTests(unittest.TestCase):
    def test_accepts_one_equivalent_numeric_value(self):
        samples = (
            "#datatype,string,long,double\n,result,table,_value\n,,0,42.5\n",
            "#datatype,string,long,double\r\n,result,table,_value\r\n,,0,4.25e1\r\n",
            '#datatype,string,long,double\n,result,table,_value\n,,0,"42.500"\n',
        )
        with tempfile.TemporaryDirectory() as raw_root:
            path = Path(raw_root) / "query.csv"
            for raw_csv in samples:
                with self.subTest(raw_csv=raw_csv):
                    path.write_bytes(raw_csv.encode("utf-8"))
                    report = influx_value_check.verify_known_value(str(path), "42.5")
                    self.assertEqual(report["numeric_row_count"], 1)

    def test_rejects_missing_wrong_duplicate_and_nonfinite_values(self):
        samples = (
            "#datatype,string,long,double\n,result,table,_value\n",
            "#datatype,string,long,double\n,result,table,_value\n,,0,41.5\n",
            "#datatype,string,long,double\n,result,table,_value\n,,0,42.5\n,,0,42.5\n",
            "#datatype,string,long,double\n,result,table,_value\n,,0,NaN\n",
        )
        with tempfile.TemporaryDirectory() as raw_root:
            path = Path(raw_root) / "query.csv"
            for raw_csv in samples:
                with self.subTest(raw_csv=raw_csv):
                    path.write_text(raw_csv, encoding="utf-8")
                    with self.assertRaises(influx_value_check.ValueCheckError):
                        influx_value_check.verify_known_value(str(path), "42.5")

    def test_rejects_symlinked_query_result(self):
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            target = root / "target.csv"
            target.write_text(
                "#datatype,string,long,double\n,result,table,_value\n,,0,42.5\n",
                encoding="utf-8",
            )
            link = root / "query.csv"
            link.symlink_to(target)
            with self.assertRaises(influx_value_check.ValueCheckError):
                influx_value_check.verify_known_value(str(link), "42.5")

    def test_rejects_same_inode_mutation_even_when_mtime_is_restored(self):
        bad_csv = "#datatype,string,long,double\n,result,table,_value\n,,0,41.5\n"
        good_csv = "#datatype,string,long,double\n,result,table,_value\n,,0,42.5\n"
        self.assertEqual(len(bad_csv), len(good_csv))
        with tempfile.TemporaryDirectory() as raw_root:
            path = Path(raw_root) / "query.csv"
            path.write_text(bad_csv, encoding="utf-8")
            original = path.stat()
            real_read = os.read
            mutated = False

            def mutate_before_read(descriptor, size):
                nonlocal mutated
                if not mutated:
                    mutated = True
                    with path.open("r+b", buffering=0) as stream:
                        stream.write(good_csv.encode("utf-8"))
                        os.fsync(stream.fileno())
                    os.utime(
                        path,
                        ns=(original.st_atime_ns, original.st_mtime_ns),
                    )
                return real_read(descriptor, size)

            with mock.patch.object(
                influx_value_check.os,
                "read",
                side_effect=mutate_before_read,
            ), self.assertRaisesRegex(
                influx_value_check.ValueCheckError,
                "changed while it was read",
            ):
                influx_value_check.verify_known_value(str(path), "42.5")

            self.assertTrue(mutated)
            self.assertEqual(path.stat().st_ino, original.st_ino)
            self.assertEqual(path.stat().st_mtime_ns, original.st_mtime_ns)
            self.assertNotEqual(path.stat().st_ctime_ns, original.st_ctime_ns)


class EvidenceIOTests(unittest.TestCase):
    def test_atomic_creation_rejects_preexisting_directory(self):
        project = "m0ci-run-123456"
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            os.chmod(root, 0o700)
            evidence = root / f"evidence-{project}"
            evidence.mkdir(mode=0o700)
            marker = evidence / "keep"
            marker.write_bytes(b"preexisting")
            with self.assertRaises(evidence_io.EvidenceError):
                evidence_io.create_evidence_directory(
                    str(evidence), str(root), project
                )
            self.assertEqual(marker.read_bytes(), b"preexisting")

    def test_exclusive_capture_refuses_symlink_without_truncating_victim(self):
        project = "m0ci-run-123456"
        with tempfile.TemporaryDirectory() as raw_root:
            allowed = Path(raw_root)
            os.chmod(allowed, 0o700)
            root, descriptor, identity = evidence_io.create_evidence_directory(
                str(allowed / f"evidence-{project}"), str(allowed), project
            )
            victim = allowed / "victim"
            victim.write_bytes(b"must-not-change")
            (root / "report.json").symlink_to(victim)
            try:
                with mock.patch.object(
                    evidence_io.subprocess, "run"
                ) as run, self.assertRaises(evidence_io.EvidenceError):
                    evidence_io.capture_output(
                        root,
                        descriptor,
                        identity,
                        "report.json",
                        [sys.executable, "-c", "print('attacker')"],
                    )
                run.assert_not_called()
            finally:
                os.close(descriptor)
            self.assertEqual(victim.read_bytes(), b"must-not-change")

    def test_held_directory_descriptor_rejects_path_replacement(self):
        project = "m0ci-run-123456"
        with tempfile.TemporaryDirectory() as raw_root:
            allowed = Path(raw_root)
            os.chmod(allowed, 0o700)
            root, descriptor, identity = evidence_io.create_evidence_directory(
                str(allowed / f"evidence-{project}"), str(allowed), project
            )
            moved = allowed / "held-evidence"
            root.rename(moved)
            root.mkdir(mode=0o700)
            try:
                with self.assertRaises(evidence_io.EvidenceError):
                    evidence_io.capture_output(
                        root,
                        descriptor,
                        identity,
                        "report.json",
                        [sys.executable, "-c", "print('unexpected')"],
                    )
            finally:
                os.close(descriptor)
            self.assertFalse((root / "report.json").exists())
            self.assertFalse((moved / "report.json").exists())

    def test_create_exec_preserves_guard_fd_for_exclusive_child_capture(self):
        project = "m0ci-run-123456"
        with tempfile.TemporaryDirectory() as raw_root:
            allowed = Path(raw_root)
            os.chmod(allowed, 0o700)
            root = allowed / f"evidence-{project}"
            script = allowed / "guard-child.sh"
            script.write_text(
                "#!/usr/bin/env bash\n"
                "set -eu\n"
                'test "$M0_EVIDENCE_GUARD_PID" = "$$"\n'
                f'"{sys.executable}" "{RECOVERY_ROOT / "evidence_io.py"}" capture '
                f'--root "{root}" --fd "$M0_EVIDENCE_DIR_FD" '
                '--device "$M0_EVIDENCE_DEVICE" --inode "$M0_EVIDENCE_INODE" '
                '--name inherited.txt -- /usr/bin/printf inherited\n',
                encoding="utf-8",
            )
            script.chmod(0o700)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(RECOVERY_ROOT / "evidence_io.py"),
                    "create-and-exec",
                    "--path",
                    str(root),
                    "--allowed-root",
                    str(allowed),
                    "--project",
                    project,
                    "--script",
                    str(script),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual((root / "inherited.txt").read_bytes(), b"inherited")
            self.assertEqual((root / "inherited.txt").stat().st_mode & 0o777, 0o600)


class DinDTLSTests(unittest.TestCase):
    PEM = b"-----BEGIN TEST-----\nvalue\n-----END TEST-----\n"

    def _capture_command(self):
        return [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(" + repr(self.PEM) + ")",
        ]

    def test_client_material_is_private_regular_pem(self):
        with tempfile.TemporaryDirectory() as raw_root:
            allowed = Path(raw_root)
            os.chmod(allowed, 0o700)
            path = allowed / ".m0ci-run-123456.dind-client"
            identity = dind_tls.create_directory(str(path), str(allowed))
            for name in dind_tls.REQUIRED_FILES:
                dind_tls.capture_client_file(
                    path,
                    identity,
                    name,
                    self._capture_command(),
                )
            dind_tls.secure_client_files(path, identity)
            for name in dind_tls.REQUIRED_FILES:
                self.assertEqual((path / name).stat().st_mode & 0o777, 0o600)
                self.assertEqual((path / name).stat().st_nlink, 1)
                self.assertEqual((path / name).read_bytes(), self.PEM)
            dind_tls.cleanup_directory(path, identity)
            self.assertFalse(path.exists())

    def test_capture_rejects_precreated_symlink_before_command(self):
        with tempfile.TemporaryDirectory() as raw_root:
            allowed = Path(raw_root)
            os.chmod(allowed, 0o700)
            path = allowed / ".m0ci-run-123456.dind-client"
            identity = dind_tls.create_directory(str(path), str(allowed))
            victim = allowed / "victim-key"
            victim.write_text("private-victim", encoding="utf-8")
            os.chmod(victim, 0o640)
            (path / "key.pem").symlink_to(victim)
            with mock.patch.object(dind_tls.subprocess, "run") as run:
                with self.assertRaises(dind_tls.TLSMaterialError):
                    dind_tls.capture_client_file(
                        path, identity, "key.pem", self._capture_command()
                    )
                run.assert_not_called()
            self.assertEqual(victim.read_text(encoding="utf-8"), "private-victim")
            self.assertEqual(victim.stat().st_mode & 0o777, 0o640)

    def test_capture_rejects_precreated_hardlink_before_command(self):
        with tempfile.TemporaryDirectory() as raw_root:
            allowed = Path(raw_root)
            os.chmod(allowed, 0o700)
            path = allowed / ".m0ci-run-123456.dind-client"
            identity = dind_tls.create_directory(str(path), str(allowed))
            victim = allowed / "victim-cert"
            victim.write_bytes(b"must-not-change")
            os.chmod(victim, 0o640)
            os.link(victim, path / "cert.pem")
            with mock.patch.object(dind_tls.subprocess, "run") as run:
                with self.assertRaises(dind_tls.TLSMaterialError):
                    dind_tls.capture_client_file(
                        path, identity, "cert.pem", self._capture_command()
                    )
                run.assert_not_called()
            self.assertEqual(victim.read_bytes(), b"must-not-change")
            self.assertEqual(victim.stat().st_mode & 0o777, 0o640)


class WorkflowBoundaryTests(unittest.TestCase):
    def test_runner_local_evidence_has_no_artifact_uploader(self):
        workflow = (
            RECOVERY_ROOT.parents[1] / ".github/workflows/m0-recovery-drill.yml"
        ).read_text(encoding="utf-8")
        self.assertNotIn("actions/upload-artifact", workflow)
        self.assertNotIn("M0_EVIDENCE_PATH", workflow)


class ArchiveTests(unittest.TestCase):
    def _fake_docker(self, root: Path) -> Path:
        executable = root / "fake-docker"
        executable.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "case \" $* \" in\n"
            "  *\" image save \"*) printf 'held-fd-image-archive' ;;\n"
            "  *\" image load \"*) command cat >/dev/null ;;\n"
            "  *\" image inspect \"*) : ;;\n"
            "  *) exit 12 ;;\n"
            "esac\n"
        )
        executable.chmod(0o700)
        return executable

    def test_rejects_overwrite_and_unsafe_parent(self):
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            os.chmod(root, 0o700)
            target = root / "images.tar"
            target.write_bytes(b"existing")
            with self.assertRaises(archive.ArchiveError):
                archive._validate_new_file(str(target), ".tar")
            target.unlink()
            os.chmod(root, 0o777)
            with self.assertRaises(archive.ArchiveError):
                archive._validate_new_file(str(target), ".tar")

    def test_hashes_the_held_descriptor(self):
        with tempfile.TemporaryDirectory() as raw_root:
            path = Path(raw_root) / "archive.tar"
            path.write_bytes(b"known archive bytes")
            descriptor, identity = archive._open_regular(path)
            try:
                self.assertEqual(
                    archive._sha256_fd(descriptor, identity),
                    hashlib.sha256(b"known archive bytes").hexdigest(),
                )
            finally:
                os.close(descriptor)

    def test_docker_tls_client_options_are_complete_and_explicit(self):
        arguments = argparse.Namespace(
            docker_bin="docker",
            docker_host="tcp://127.0.0.1:42376",
            docker_tls_ca="/private/ca.pem",
            docker_tls_cert="/private/cert.pem",
            docker_tls_key="/private/key.pem",
        )
        command = archive._docker_base_command(arguments)
        self.assertEqual(command[0:3], ["docker", "--host", arguments.docker_host])
        self.assertIn("--tlsverify", command)
        self.assertEqual(command[command.index("--tlskey") + 1], "/private/key.pem")

        arguments.docker_tls_key = None
        with self.assertRaisesRegex(archive.ArchiveError, "required together"):
            archive._docker_base_command(arguments)

    def test_fake_docker_archive_save_and_independent_load(self):
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            os.chmod(root, 0o700)
            docker = self._fake_docker(root)
            ids = ["sha256:" + character * 64 for character in "abc"]
            archive_path = root / "images.tar"
            checksum_path = root / "images.json"
            saved = archive.save_archive(
                argparse.Namespace(
                    image_id=ids,
                    path=str(archive_path),
                    checksum=str(checksum_path),
                    docker_bin=str(docker),
                    timeout=10.0,
                )
            )
            self.assertEqual(saved["image_ids"], ids)
            loaded = archive.load_archive(
                argparse.Namespace(
                    path=str(archive_path),
                    checksum=str(checksum_path),
                    docker_bin=str(docker),
                    docker_host="tcp://127.0.0.1:42376",
                    timeout=10.0,
                )
            )
            self.assertEqual(loaded["image_ids"], ids)

            with archive_path.open("ab") as stream:
                stream.write(b"tamper")
            with self.assertRaises(archive.ArchiveError):
                archive.load_archive(
                    argparse.Namespace(
                        path=str(archive_path),
                        checksum=str(checksum_path),
                        docker_bin=str(docker),
                        docker_host="tcp://127.0.0.1:42376",
                        timeout=10.0,
                    )
                )

    def test_stage_path_replacement_cannot_redirect_fd_publication(self):
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            os.chmod(root, 0o700)
            docker = self._fake_docker(root)
            ids = ["sha256:" + character * 64 for character in "abc"]
            archive_path = root / "images.tar"
            checksum_path = root / "images.json"
            moved_stage = root / "held-stage-moved"
            attacker_bytes = b"attacker-path-content"
            held_bytes = b"held-fd-image-archive"
            real_publish = archive._platform_publish_fd
            replaced_path = None

            def replace_stage_then_publish(source_fd, parent_fd, destination_name):
                nonlocal replaced_path
                if destination_name == archive_path.name:
                    stages = list(root.glob(".m0-images-*.tar"))
                    self.assertEqual(len(stages), 1)
                    replaced_path = stages[0]
                    stages[0].rename(moved_stage)
                    stages[0].write_bytes(attacker_bytes)
                return real_publish(source_fd, parent_fd, destination_name)

            with mock.patch.object(
                archive,
                "_platform_publish_fd",
                side_effect=replace_stage_then_publish,
            ):
                archive.save_archive(
                    argparse.Namespace(
                        image_id=ids,
                        path=str(archive_path),
                        checksum=str(checksum_path),
                        docker_bin=str(docker),
                        timeout=10.0,
                    )
                )

            self.assertIsNotNone(replaced_path)
            self.assertEqual(replaced_path.read_bytes(), attacker_bytes)
            self.assertEqual(moved_stage.read_bytes(), held_bytes)
            self.assertEqual(archive_path.read_bytes(), held_bytes)

    def test_mutate_restore_cannot_evade_docker_stream_digest(self):
        expected = b"GOODSAFE"
        attacker = b"EVIL"
        with tempfile.TemporaryDirectory() as raw_root:
            path = Path(raw_root) / "archive.tar"
            path.write_bytes(expected)
            original_stat = path.stat()
            descriptor, identity = archive._open_regular(path)
            completed = threading.Event()
            consumed = bytearray()

            class MutatingInput:
                closed = False

                def write(self, data):
                    block = bytes(data)
                    consumed.extend(block)
                    if len(consumed) == 4:
                        writer = os.open(path, os.O_WRONLY)
                        try:
                            os.pwrite(writer, attacker, 4)
                            os.fsync(writer)
                        finally:
                            os.close(writer)
                    elif len(consumed) == 8:
                        writer = os.open(path, os.O_WRONLY)
                        try:
                            os.pwrite(writer, expected[4:], 4)
                            os.fsync(writer)
                        finally:
                            os.close(writer)
                        os.utime(
                            path,
                            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                        )
                    return len(block)

                def close(self):
                    self.closed = True
                    completed.set()

            class FakeProcess:
                def __init__(self):
                    self.stdin = MutatingInput()

                def wait(self, timeout=None):
                    if not completed.wait(timeout):
                        raise subprocess.TimeoutExpired(["fake-docker"], timeout)
                    return 0

                def kill(self):
                    completed.set()

            try:
                with mock.patch.object(
                    archive.subprocess, "Popen", return_value=FakeProcess()
                ), self.assertRaisesRegex(
                    archive.ArchiveError,
                    "bytes delivered to Docker do not match archive checksum",
                ):
                    archive._stream_load_to_docker(
                        ["fake-docker", "image", "load"],
                        descriptor,
                        identity,
                        hashlib.sha256(expected).hexdigest(),
                        10.0,
                        chunk_size=4,
                    )
            finally:
                os.close(descriptor)
            self.assertEqual(bytes(consumed), b"GOODEVIL")
            self.assertEqual(path.read_bytes(), expected)


class SmokeHelpersTests(unittest.TestCase):
    def test_result_list_supports_paginated_and_plain_shapes(self):
        self.assertEqual(smoke.result_list([{"id": 1}]), [{"id": 1}])
        self.assertEqual(smoke.result_list({"results": [{"id": 2}]}), [{"id": 2}])
        with self.assertRaises(smoke.SmokeError):
            smoke.result_list({"data": []})

    def test_websocket_rejects_non_loopback_target(self):
        with self.assertRaises(smoke.SmokeError):
            smoke.websocket_first_message("http://example.com:80")

    def test_celery_reports_bind_exact_node_and_queue(self):
        node = "m0-acq@m0ci-run-123456-acq"
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            ping = root / "ping.json"
            queues = root / "queues.json"
            ping.write_text(
                '{"m0-acq@m0ci-run-123456-acq":{"ok":"pong"}}',
                encoding="utf-8",
            )
            queues.write_text(
                '{"m0-acq@m0ci-run-123456-acq":[{"name":"acquisition"}]}',
                encoding="utf-8",
            )
            self.assertEqual(
                celery_smoke.validate_reports(ping, queues, node, "acquisition"),
                {"node": node, "ping": "pong", "active_queue": "acquisition"},
            )

            ping.write_text(
                '{"m0-acq@m0ci-run-123456-acq":{"ok":"pong"},'
                '"unexpected@worker":{"ok":"pong"}}',
                encoding="utf-8",
            )
            with self.assertRaises(celery_smoke.CelerySmokeError):
                celery_smoke.validate_reports(ping, queues, node, "acquisition")

            ping.write_text(
                '{"m0-acq@m0ci-run-123456-acq":{"ok":"pong"}}',
                encoding="utf-8",
            )
            queues.write_text(
                '{"m0-acq@m0ci-run-123456-acq":[{"name":"short"}]}',
                encoding="utf-8",
            )
            with self.assertRaises(celery_smoke.CelerySmokeError):
                celery_smoke.validate_reports(ping, queues, node, "acquisition")


if __name__ == "__main__":
    unittest.main()
