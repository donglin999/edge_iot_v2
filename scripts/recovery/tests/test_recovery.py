from __future__ import annotations

import hashlib
import importlib.util
import argparse
import os
import tempfile
import unittest
from pathlib import Path


RECOVERY_ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, RECOVERY_ROOT / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


safety = load_module("m0_safety", "safety.py")
archive = load_module("m0_image_archive", "image_archive.py")
smoke = load_module("m0_stack_smoke", "stack_smoke.py")


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
                    docker_host="tcp://127.0.0.1:2375",
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
                        docker_host="tcp://127.0.0.1:2375",
                        timeout=10.0,
                    )
                )


class SmokeHelpersTests(unittest.TestCase):
    def test_result_list_supports_paginated_and_plain_shapes(self):
        self.assertEqual(smoke.result_list([{"id": 1}]), [{"id": 1}])
        self.assertEqual(smoke.result_list({"results": [{"id": 2}]}), [{"id": 2}])
        with self.assertRaises(smoke.SmokeError):
            smoke.result_list({"data": []})

    def test_websocket_rejects_non_loopback_target(self):
        with self.assertRaises(smoke.SmokeError):
            smoke.websocket_first_message("http://example.com:80")


if __name__ == "__main__":
    unittest.main()
