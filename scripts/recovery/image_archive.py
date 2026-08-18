#!/usr/bin/env python3
"""Create and load a SHA-bound Docker image archive through held file descriptors."""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
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


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _regular_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _assert_parent_identity(
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
    ):
        raise ArchiveError("archive parent changed during held-fd operation")


def _open_parent(path: Path, expected: tuple[int, int]) -> int:
    descriptor = os.open(path, _directory_flags())
    try:
        _assert_parent_identity(path, descriptor, expected)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


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
    if not stat.S_ISREG(before.st_mode) or _identity(before) != expected:
        raise ArchiveError("held archive descriptor changed identity")
    digest = hashlib.sha256()
    offset = 0
    while offset < before.st_size:
        block = os.pread(descriptor, min(1024 * 1024, before.st_size - offset), offset)
        if not block:
            raise ArchiveError("held archive ended while hashing")
        digest.update(block)
        offset += len(block)
    after = os.fstat(descriptor)
    if (
        _identity(after) != expected
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
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


def _stream_load_to_docker(
    command: list[str],
    descriptor: int,
    expected_identity: tuple[int, int],
    expected_digest: str,
    timeout: float,
    *,
    chunk_size: int = 256 * 1024,
) -> str:
    """Hash exactly the bytes concurrently delivered to ``docker image load``."""
    if chunk_size <= 0:
        raise ArchiveError("Docker stream chunk size must be positive")
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode) or _identity(before) != expected_identity:
        raise ArchiveError("held archive descriptor changed before Docker load")
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise ArchiveError(
            f"Docker command could not complete: {type(exc).__name__}"
        ) from None
    if process.stdin is None:  # pragma: no cover - guaranteed by PIPE.
        process.kill()
        process.wait()
        raise ArchiveError("Docker load stdin pipe was not created")

    errors: list[BaseException] = []
    streamed_digest: list[str] = []

    def pump() -> None:
        digest = hashlib.sha256()
        offset = 0
        try:
            while offset < before.st_size:
                block = os.pread(
                    descriptor,
                    min(chunk_size, before.st_size - offset),
                    offset,
                )
                if not block:
                    raise ArchiveError("held archive ended during Docker load")
                digest.update(block)
                view = memoryview(block)
                while view:
                    written = process.stdin.write(view)
                    if written is None or written <= 0:
                        raise ArchiveError("Docker load stopped consuming archive bytes")
                    view = view[written:]
                offset += len(block)
            process.stdin.close()
            after = os.fstat(descriptor)
            if (
                _identity(after) != expected_identity
                or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
            ):
                raise ArchiveError("archive changed while streaming to Docker")
            streamed_digest.append(digest.hexdigest())
        except BaseException as exc:  # Propagate writer-thread failures below.
            errors.append(exc)
            try:
                process.stdin.close()
            except OSError:
                pass

    writer = threading.Thread(target=pump, name="m0-docker-load-stream", daemon=True)
    writer.start()
    try:
        return_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        writer.join(timeout=5)
        raise ArchiveError("Docker command could not complete: TimeoutExpired") from None
    writer.join(timeout=5)
    if writer.is_alive():
        process.kill()
        process.wait()
        raise ArchiveError("Docker archive writer did not terminate")
    if errors:
        error = errors[0]
        if isinstance(error, ArchiveError):
            raise error
        raise ArchiveError(
            f"Docker load stream failed: {type(error).__name__}"
        ) from None
    if return_code != 0:
        raise ArchiveError("Docker command failed; output suppressed")
    if streamed_digest != [expected_digest]:
        raise ArchiveError("bytes delivered to Docker do not match archive checksum")
    return streamed_digest[0]


def _docker_base_command(args: argparse.Namespace) -> list[str]:
    command = [args.docker_bin]
    if args.docker_host:
        command += ["--host", args.docker_host]
    tls_values = (
        getattr(args, "docker_tls_ca", None),
        getattr(args, "docker_tls_cert", None),
        getattr(args, "docker_tls_key", None),
    )
    if any(tls_values):
        if not all(tls_values):
            raise ArchiveError("all Docker TLS client files are required together")
        if not args.docker_host:
            raise ArchiveError("Docker TLS client files require an explicit host")
        command += [
            "--tlsverify",
            "--tlscacert",
            tls_values[0],
            "--tlscert",
            tls_values[1],
            "--tlskey",
            tls_values[2],
        ]
    return command


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
        _call_linkat(source_descriptor, b"", parent_descriptor, encoded_name, 0x1000)
        return
    except FileExistsError:
        raise
    except OSError as direct_error:
        if not Path("/proc/self/fd").is_dir():
            raise direct_error
        try:
            _call_linkat(
                -100,
                os.fsencode(f"/proc/self/fd/{source_descriptor}"),
                parent_descriptor,
                encoded_name,
                0x400,
            )
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
    # This is the same fd-authoritative no-replace strategy used and heavily
    # race-tested by scripts/backup/sqlite_backup.py.
    if sys.platform.startswith("linux"):
        _linux_publish_fd(source_descriptor, parent_descriptor, destination_name)
        return
    if sys.platform == "darwin":
        _darwin_publish_fd(source_descriptor, parent_descriptor, destination_name)
        return
    raise OSError(errno.ENOTSUP, "fd-based no-replace publication is unsupported")


def _publish_held_file(
    descriptor: int,
    expected_identity: tuple[int, int],
    expected_digest: str,
    target: Path,
    parent_descriptor: int,
    parent_identity: tuple[int, int],
) -> None:
    """Publish the held inode, never the mutable mkstemp pathname."""
    _assert_parent_identity(target.parent, parent_descriptor, parent_identity)
    if _sha256_fd(descriptor, expected_identity) != expected_digest:
        raise ArchiveError("held staging digest changed before publication")
    try:
        _platform_publish_fd(descriptor, parent_descriptor, target.name)
    except FileExistsError as exc:
        raise ArchiveError("refusing to overwrite archive output") from exc
    except OSError as exc:
        raise ArchiveError(f"fd-safe no-replace publication failed: {exc}") from exc

    published_descriptor: Optional[int] = None
    try:
        published_descriptor = os.open(
            target.name, _regular_flags(), dir_fd=parent_descriptor
        )
        published_stat = os.fstat(published_descriptor)
        path_stat = os.stat(
            target.name, dir_fd=parent_descriptor, follow_symlinks=False
        )
        published_identity = _identity(published_stat)
        if (
            not stat.S_ISREG(published_stat.st_mode)
            or _identity(path_stat) != published_identity
        ):
            raise ArchiveError("published path did not bind to one regular file")
        if _sha256_fd(published_descriptor, published_identity) != expected_digest:
            raise ArchiveError("published file digest does not match held staging file")
        os.fsync(published_descriptor)
    finally:
        if published_descriptor is not None:
            os.close(published_descriptor)
    os.fsync(parent_descriptor)
    _assert_parent_identity(target.parent, parent_descriptor, parent_identity)


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
    parent_descriptor = _open_parent(archive.parent, parent_identity)

    descriptor: Optional[int] = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".m0-images-", suffix=".tar", dir=archive.parent
        )
        stage = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        stage_identity = _identity(os.fstat(descriptor))
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
        _publish_held_file(
            descriptor,
            stage_identity,
            digest,
            archive,
            parent_descriptor,
            parent_identity,
        )

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
            checksum_digest = _sha256_fd(checksum_descriptor, checksum_identity)
            _publish_held_file(
                checksum_descriptor,
                checksum_identity,
                checksum_digest,
                checksum,
                parent_descriptor,
                parent_identity,
            )
        finally:
            os.close(checksum_descriptor)
        return {"operation": "save", "archive_sha256": digest, "image_ids": list(image_ids)}
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_descriptor)


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
        command = _docker_base_command(args) + ["image", "load"]
        _stream_load_to_docker(
            command,
            archive_fd,
            archive_identity,
            digest,
            args.timeout,
        )
        for image_id in image_ids:
            inspect = _docker_base_command(args) + ["image", "inspect", image_id]
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
            child.add_argument("--docker-tls-ca")
            child.add_argument("--docker-tls-cert")
            child.add_argument("--docker-tls-key")
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
