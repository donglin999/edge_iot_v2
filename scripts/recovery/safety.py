#!/usr/bin/env python3
"""Fail-closed safety checks for the disposable M0 recovery drill."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Iterable, Optional, Sequence


PROJECT_RE = re.compile(r"^m0ci-[a-z0-9](?:[a-z0-9-]{6,46}[a-z0-9])$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
ISOLATED_GATEWAY_OPTION = "com.docker.network.bridge.gateway_mode_ipv4"
RECOVERY_SCRIPT_SHA256 = "e1d16e506d9ced175f8b64b1dcb178a10ba4956bafada5a710748fac1ff4490b"
INFLUX_CLI_URL = (
    "https://dl.influxdata.com/influxdb/releases/"
    "influxdb2-client-2.7.5-linux-amd64.tar.gz"
)
INFLUX_CLI_SHA256 = (
    "496dffcd70bed2bb3dc3d614e3d9c97e312e092dfe0577d332027566bbb7d8cd"
)
EXPECTED_RECOVERY_TOOL_INSTRUCTIONS = (
    "FROM python:3.10-slim AS influx-cli",
    "ARG TARGETARCH",
    (
        'RUN test "$TARGETARCH" = "amd64" '
        "&& apt-get update "
        "&& apt-get install -y --no-install-recommends ca-certificates curl "
        "&& rm -rf /var/lib/apt/lists/* "
        '&& archive="/tmp/influxdb2-client-2.7.5-linux-amd64.tar.gz" '
        "&& curl --fail --show-error --silent --location "
        f'"{INFLUX_CLI_URL}" '
        '--output "$archive" '
        "&& printf '%s  %s\\n' "
        f'"{INFLUX_CLI_SHA256}" '
        '"$archive" | sha256sum --check --strict - '
        '&& tar -xzf "$archive" -C /tmp ./influx '
        "&& install -m 0755 /tmp/influx /usr/local/bin/influx "
        "&& /usr/local/bin/influx version "
        '&& rm -f "$archive" /tmp/influx'
    ),
    "FROM python:3.10-slim",
    "COPY --from=influx-cli /usr/local/bin/influx /usr/local/bin/influx",
    "WORKDIR /repo",
    "COPY scripts/backup/ /repo/scripts/backup/",
    "ENTRYPOINT []",
    'CMD ["python", "--version"]',
)
FORBIDDEN_PROJECTS = {
    "edge_iot_v2",
    "edge-iot-v2",
    "edge_iot_v2-remediation",
    "edge-iot",
    "production",
}


class SafetyError(RuntimeError):
    """The drill cannot prove that its target is disposable and isolated."""


def validate_project(project: str) -> str:
    if project in FORBIDDEN_PROJECTS or PROJECT_RE.fullmatch(project) is None:
        raise SafetyError(
            "project must be a unique disposable name matching "
            "m0ci-[a-z0-9-] (9-49 characters)"
        )
    return project


def validate_image_ids(values: Iterable[str]) -> tuple[str, ...]:
    image_ids = tuple(values)
    if not image_ids or any(IMAGE_ID_RE.fullmatch(value) is None for value in image_ids):
        raise SafetyError("every image reference must be an immutable sha256 ID")
    return image_ids


def _is_real_directory(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)


def validate_new_evidence_path(path_value: str, allowed_root_value: str, project: str) -> Path:
    validate_project(project)
    path = Path(path_value)
    allowed_root = Path(allowed_root_value)
    if not path.is_absolute() or not allowed_root.is_absolute():
        raise SafetyError("evidence and allowed root must be absolute paths")
    if not _is_real_directory(allowed_root):
        raise SafetyError("allowed evidence root must be a real existing directory")
    resolved_root = allowed_root.resolve(strict=True)
    if resolved_root in {Path("/"), Path.home().resolve(), Path.cwd().resolve()}:
        raise SafetyError("refusing a broad evidence root")
    if path.exists() or path.is_symlink():
        raise SafetyError("evidence path must not already exist")
    try:
        resolved_parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise SafetyError("evidence parent must already exist") from exc
    if resolved_parent != resolved_root:
        raise SafetyError("evidence must be a direct child of the allowed root")
    if project not in path.name:
        raise SafetyError("evidence directory name must contain the disposable project")
    return path


def validate_created_private_directory(path_value: str) -> Path:
    path = Path(path_value)
    if not path.is_absolute() or not _is_real_directory(path):
        raise SafetyError("evidence directory must be an absolute real directory")
    metadata = os.lstat(path)
    if metadata.st_uid != os.geteuid():
        raise SafetyError("evidence directory must be owned by the current user")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise SafetyError("evidence directory mode must be exactly 0700")
    return path


def validate_compose_source(path_value: str) -> Path:
    path = Path(path_value).resolve(strict=True)
    text = path.read_text(encoding="utf-8")
    forbidden = {
        "container_name:": "explicit container names",
        "network_mode:": "non-bridge network mode",
        "external: true": "external resources",
        "external: {": "external resources",
        "build:": "an in-drill image build",
        "/var/run/docker.sock": "the host Docker socket",
    }
    for marker, description in forbidden.items():
        if marker in text:
            raise SafetyError(f"Compose source contains {description}: {marker}")
    image_count = text.count("\n    image:")
    no_pull_count = text.count("\n    pull_policy: never")
    if image_count == 0 or no_pull_count != image_count:
        raise SafetyError("every drill image must be protected by pull_policy: never")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("ports:", "published:", "host_ip:")):
            raise SafetyError("the internal drill stack must not publish host ports")
    if "driver: bridge" not in text or "internal: true" not in text:
        raise SafetyError("drill network must be an internal bridge")
    if "enable_ipv6: false" not in text:
        raise SafetyError("drill network must explicitly disable IPv6")
    if f"{ISOLATED_GATEWAY_OPTION}: isolated" not in text:
        raise SafetyError("drill network must remove the host bridge gateway")
    credential_keys = {
        "SECRET_KEY",
        "INFLUXDB_TOKEN",
        "DOCKER_INFLUXDB_INIT_PASSWORD",
        "DOCKER_INFLUXDB_INIT_ADMIN_TOKEN",
    }
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        if key in credential_keys and not value.strip().startswith("${"):
            raise SafetyError(f"Compose source contains a credential literal: {key}")
    return path


def _strip_shell_comment(line: str) -> str:
    """Remove a shell comment without treating quoted # characters as comments."""
    quote: Optional[str] = None
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if quote == "'":
            if character == "'":
                quote = None
            continue
        if character == "\\":
            escaped = True
            continue
        if quote == '"':
            if character == '"':
                quote = None
            continue
        if character in ("'", '"'):
            quote = character
            continue
        if character == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index]
    return line


def _shell_without_heredocs(text: str) -> str:
    outside: list[str] = []
    delimiter: Optional[str] = None
    strip_tabs = False
    heredoc = re.compile(r"<<(-?)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
    for raw_line in text.splitlines(keepends=True):
        comparable = raw_line.rstrip("\r\n")
        if delimiter is not None:
            candidate = comparable.lstrip("\t") if strip_tabs else comparable
            if candidate == delimiter:
                delimiter = None
                strip_tabs = False
            continue
        outside.append(raw_line)
        code = _strip_shell_comment(comparable)
        matches = tuple(heredoc.finditer(code))
        if "<<" in code.replace("<<<", "") and not matches:
            raise SafetyError("recovery script contains an unsupported heredoc form")
        if len(matches) > 1:
            raise SafetyError("recovery script contains multiple heredocs in one command")
        if matches:
            strip_tabs = matches[0].group(1) == "-"
            delimiter = matches[0].group(3)
    if delimiter is not None:
        raise SafetyError("recovery script contains an unterminated heredoc")
    return "".join(outside)


def _shell_logical_code(text: str) -> tuple[str, ...]:
    joined = _shell_without_heredocs(text).replace("\\\n", " ")
    return tuple(
        code.strip()
        for raw_line in joined.splitlines()
        if (code := _strip_shell_comment(raw_line)).strip()
    )


def _validate_direct_dind_run(line: str) -> None:
    """Allow one literal DinD launch whose only daemon listener is Unix."""
    normalized = " ".join(line.split())
    without_approved_host = normalized.replace(
        "--host=unix:///var/run/docker.sock", ""
    )
    forbidden = (
        r"(?:^|\s)-p[^\s]*",
        r"(?:^|\s)--publish(?:\s|=|$)",
        r"(?:^|\s)-P(?:\s|$)",
        r"(?:^|\s)--publish-all(?:\s|=|$)",
        r"(?:^|\s)-H[^\s]*",
        r"(?:^|\s)--host(?:\s|=|$)",
        r"(?:^|\s)DOCKER_HOST\s*=",
        r"(?:^|\s)--network(?:\s+|=)host(?:\s|$)",
        r"(?:^|\s)--entrypoint(?:\s|=|$)",
        r"\beval\b",
        r"\balias\b",
        r"\b2375\b",
        r"\b2376\b",
        r"tcp://",
    )
    if any(re.search(pattern, without_approved_host) for pattern in forbidden):
        raise SafetyError("recovery script exposes DinD instead of using internal exec")
    expected = (
        'DIND_ID="$(docker run -d --pull=never --privileged --name "$DIND_NAME" '
        '--label "com.edge-iot.m0.project=$PROJECT" '
        '--network "$DIND_NETWORK_ID" '
        '--memory 768m --cpus 1.50 --pids-limit 512 '
        '-e DOCKER_TLS_CERTDIR= '
        '"$M0_DIND_IMAGE_ID" dockerd --host=unix:///var/run/docker.sock)"'
    )
    if normalized != expected:
        raise SafetyError("recovery script exposes DinD instead of using internal exec")


def _validate_recovery_script_structure(text: str) -> None:
    broad_cleanup = re.compile(
        r"\bdocker\s+(?:system|image|volume|network)\s+prune\b"
        r"|\bdocker\s+compose\s+down\b"
    )
    unsafe_evidence_redirect = re.compile(
        r">{1,2}\s*[\"']?\$\{?EVIDENCE_ROOT\}?"
    )
    direct_dind_runs: list[str] = []
    logical_lines = _shell_logical_code(text)
    code = "\n".join(logical_lines)
    if broad_cleanup.search(code):
        raise SafetyError("recovery script contains broad cleanup")
    if unsafe_evidence_redirect.search(code):
        raise SafetyError("recovery script bypasses exclusive evidence capture")
    if (
        "--tls=false" in code
        or "--docker-host" in code
        or re.search(r"\bDOCKER_HOST\s*=", code)
        or re.search(r"\bdocker\s+(?:-H\S*|--host(?:\s|=))", code)
    ):
        raise SafetyError("recovery script exposes DinD instead of using internal exec")
    if re.search(r"\bdocker[ \t]+cp\b", code):
        raise SafetyError("recovery script contains mutable-path DinD copy")
    for line in logical_lines:
        if re.search(r"\bdocker[ \t]+run\b", line):
            _validate_direct_dind_run(line)
            direct_dind_runs.append(line)
    if len(direct_dind_runs) != 1:
        raise SafetyError("recovery scripts require exactly one direct internal DinD run")


def validate_recovery_scripts(root_value: str) -> Path:
    root = Path(root_value).resolve(strict=True)
    if not root.is_dir():
        raise SafetyError("recovery script root must be a directory")
    scripts = tuple(
        path
        for path in sorted(root.rglob("*.sh"))
        if "tests" not in path.relative_to(root).parts
    )
    if tuple(path.relative_to(root).as_posix() for path in scripts) != (
        "m0_ci_drill.sh",
    ):
        raise SafetyError("recovery scripts do not match the approved executable allowlist")
    if re.fullmatch(r"[0-9a-f]{64}", RECOVERY_SCRIPT_SHA256) is None:
        raise SafetyError("approved recovery script digest is malformed")
    path = scripts[0]
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise SafetyError("approved recovery script must be one regular non-symlink file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
        if identity != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ):
            raise SafetyError("approved recovery script changed before it was opened")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    final_path = os.lstat(path)
    if identity != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) or identity != (
        final_path.st_dev,
        final_path.st_ino,
        final_path.st_size,
        final_path.st_mtime_ns,
        final_path.st_ctime_ns,
    ):
        raise SafetyError("approved recovery script changed while it was read")
    if hashlib.sha256(raw).hexdigest() != RECOVERY_SCRIPT_SHA256:
        raise SafetyError("recovery script does not match the approved source digest")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SafetyError("recovery script must be UTF-8") from exc
    _validate_recovery_script_structure(text)
    return root


def validate_dind_container(
    raw: str,
    container_id: str,
    image_id: str,
    network_id: str,
    network_name: str,
    project: str,
) -> str:
    """Validate the immutable runtime facts of the isolated DinD container."""
    validate_project(project)
    validate_image_ids((image_id,))
    if re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
        raise SafetyError("DinD container ID must be one immutable 64-character ID")
    if re.fullmatch(r"[0-9a-f]{64}", network_id) is None:
        raise SafetyError("DinD network ID must be one immutable 64-character ID")
    if network_name != f"{project}-dind-net":
        raise SafetyError("DinD network name does not match the disposable project")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SafetyError("DinD inspect JSON is invalid") from exc
    if not isinstance(document, list) or len(document) != 1 or not isinstance(document[0], dict):
        raise SafetyError("DinD inspect must contain exactly one container")
    item = document[0]
    config = item.get("Config") or {}
    host = item.get("HostConfig") or {}
    network_settings = item.get("NetworkSettings") or {}
    networks = network_settings.get("Networks") or {}
    if item.get("Id") != container_id or item.get("Image") != image_id:
        raise SafetyError("DinD immutable container or image identity mismatch")
    if item.get("Name") != f"/{project}-dind" or not (item.get("State") or {}).get("Running"):
        raise SafetyError("DinD runtime name or running state mismatch")
    if config.get("Image") != image_id:
        raise SafetyError("DinD configured image reference is not immutable")
    if config.get("Cmd") != ["dockerd", "--host=unix:///var/run/docker.sock"]:
        raise SafetyError("DinD command must expose only the Unix socket")
    if (config.get("Labels") or {}).get("com.edge-iot.m0.project") != project:
        raise SafetyError("DinD project label mismatch")
    if "DOCKER_TLS_CERTDIR=" not in (config.get("Env") or []):
        raise SafetyError("DinD TLS entrypoint environment is not explicitly disabled")
    if host.get("PublishAllPorts") is not False or host.get("PortBindings") not in (None, {}):
        raise SafetyError("DinD must not publish host ports")
    if (
        host.get("Privileged") is not True
        or host.get("Memory") != 768 * 1024 * 1024
        or host.get("NanoCpus") != 1_500_000_000
        or host.get("PidsLimit") != 512
        or host.get("Binds") not in (None, [])
    ):
        raise SafetyError("DinD runtime isolation or resource limits mismatch")
    if host.get("NetworkMode") != network_id:
        raise SafetyError("DinD host network mode is not the isolated network ID")
    if set(networks) != {network_name}:
        raise SafetyError("DinD must join exactly one project-private network")
    attachment = networks[network_name] or {}
    if attachment.get("NetworkID") != network_id:
        raise SafetyError("DinD network attachment identity mismatch")
    mounts = item.get("Mounts") or []
    docker_data_mounts = [
        mount
        for mount in mounts
        if isinstance(mount, dict) and mount.get("Destination") == "/var/lib/docker"
    ]
    if len(mounts) != 1 or len(docker_data_mounts) != 1:
        raise SafetyError("DinD must have one disposable Docker data volume")
    docker_data = docker_data_mounts[0]
    volume_name = docker_data.get("Name")
    if (
        docker_data.get("Type") != "volume"
        or not isinstance(volume_name, str)
        or re.fullmatch(r"[0-9a-f]{64}", volume_name) is None
    ):
        raise SafetyError("DinD Docker data mount is not one anonymous volume")
    return volume_name


def validate_dind_network(
    raw: str,
    network_id: str,
    network_name: str,
    container_id: str,
    project: str,
) -> None:
    """Validate the actual disposable network rather than trusting shell text."""
    validate_project(project)
    if re.fullmatch(r"[0-9a-f]{64}", network_id) is None:
        raise SafetyError("DinD network ID must be one immutable 64-character ID")
    if re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
        raise SafetyError("DinD container ID must be one immutable 64-character ID")
    if network_name != f"{project}-dind-net":
        raise SafetyError("DinD network name does not match the disposable project")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SafetyError("DinD network inspect JSON is invalid") from exc
    if not isinstance(document, list) or len(document) != 1 or not isinstance(document[0], dict):
        raise SafetyError("DinD network inspect must contain exactly one network")
    item = document[0]
    labels = item.get("Labels") or {}
    containers = item.get("Containers") or {}
    options = item.get("Options") or {}
    if item.get("Id") != network_id or item.get("Name") != network_name:
        raise SafetyError("DinD network immutable identity mismatch")
    if (
        item.get("Driver") != "bridge"
        or item.get("Scope") != "local"
        or item.get("Internal") is not True
        or item.get("EnableIPv6") is not False
        or item.get("Attachable") is not False
        or item.get("Ingress") is not False
    ):
        raise SafetyError("DinD network is not one local internal bridge")
    if labels.get("com.edge-iot.m0.project") != project:
        raise SafetyError("DinD network project label mismatch")
    if options != {ISOLATED_GATEWAY_OPTION: "isolated"}:
        raise SafetyError("DinD network does not use isolated gateway mode")
    if set(containers) != {container_id}:
        raise SafetyError("DinD network must contain exactly the isolated daemon")
    if (containers[container_id] or {}).get("Name") != f"{project}-dind":
        raise SafetyError("DinD network container name mismatch")


def validate_app_network(
    raw: str,
    network_id: str,
    redis_container_id: str,
    influx_container_id: str,
    project: str,
) -> None:
    """Prove the application bridge has no host gateway or extra members."""
    validate_project(project)
    container_ids = (redis_container_id, influx_container_id)
    if re.fullmatch(r"[0-9a-f]{64}", network_id) is None:
        raise SafetyError("application network ID must be one immutable ID")
    if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in container_ids):
        raise SafetyError("application network members must be immutable container IDs")
    if len(set(container_ids)) != 2:
        raise SafetyError("application network members must be distinct")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SafetyError("application network inspect JSON is invalid") from exc
    if not isinstance(document, list) or len(document) != 1 or not isinstance(document[0], dict):
        raise SafetyError("application network inspect must contain exactly one network")
    item = document[0]
    labels = item.get("Labels") or {}
    containers = item.get("Containers") or {}
    options = item.get("Options") or {}
    expected_name = f"{project}_drill"
    if item.get("Id") != network_id or item.get("Name") != expected_name:
        raise SafetyError("application network immutable identity mismatch")
    if (
        item.get("Driver") != "bridge"
        or item.get("Scope") != "local"
        or item.get("Internal") is not True
        or item.get("EnableIPv6") is not False
        or item.get("Attachable") is not False
        or item.get("Ingress") is not False
    ):
        raise SafetyError("application network is not one local internal bridge")
    if options != {ISOLATED_GATEWAY_OPTION: "isolated"}:
        raise SafetyError("application network does not use isolated gateway mode")
    if (
        labels.get("com.docker.compose.project") != project
        or labels.get("com.docker.compose.network") != "drill"
    ):
        raise SafetyError("application network Compose labels mismatch")
    expected_members = {
        redis_container_id: f"{project}-redis-1",
        influx_container_id: f"{project}-influx-1",
    }
    if set(containers) != set(expected_members):
        raise SafetyError("application network must contain only Redis and Influx")
    for container_id, expected_name in expected_members.items():
        if (containers[container_id] or {}).get("Name") != expected_name:
            raise SafetyError("application network member name mismatch")


def _dockerfile_instructions(text: str) -> tuple[str, ...]:
    """Return comment-free logical Dockerfile instructions."""
    instructions = []
    current = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            if re.match(r"#\s*escape\s*=", stripped, flags=re.IGNORECASE):
                raise SafetyError(
                    "recovery tool Dockerfile must use the default backslash escape"
                )
            # Docker removes pure comment lines before joining a continued
            # instruction, including comments placed inside a RUN chain.
            continue
        continued = stripped.endswith("\\")
        fragment = stripped[:-1].rstrip() if continued else stripped
        if fragment and not fragment.startswith("#"):
            current.append(fragment)
        if not continued:
            if current:
                instructions.append(" ".join(current))
            current = []
    if current:
        raise SafetyError("recovery tool Dockerfile ends with a continued instruction")
    return tuple(instructions)


def validate_recovery_tool_dockerfile(path_value: str) -> Path:
    """Require the final tool image to consume one immutable, verified CLI."""
    path = Path(path_value).resolve(strict=True)
    instructions = _dockerfile_instructions(path.read_text(encoding="utf-8"))
    if instructions != EXPECTED_RECOVERY_TOOL_INSTRUCTIONS:
        raise SafetyError(
            "recovery tool Dockerfile must exactly match the approved verified CLI flow"
        )
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    project = subparsers.add_parser("project")
    project.add_argument("value")

    images = subparsers.add_parser("images")
    images.add_argument("values", nargs="+")

    evidence = subparsers.add_parser("new-evidence")
    evidence.add_argument("--path", required=True)
    evidence.add_argument("--allowed-root", required=True)
    evidence.add_argument("--project", required=True)

    private = subparsers.add_parser("private-directory")
    private.add_argument("path")

    compose = subparsers.add_parser("compose")
    compose.add_argument("path")
    scripts = subparsers.add_parser("recovery-scripts")
    scripts.add_argument("--root", required=True)
    dind = subparsers.add_parser("dind-container")
    dind.add_argument("--container-id", required=True)
    dind.add_argument("--image-id", required=True)
    dind.add_argument("--network-id", required=True)
    dind.add_argument("--network-name", required=True)
    dind.add_argument("--project", required=True)
    dind_network = subparsers.add_parser("dind-network")
    dind_network.add_argument("--network-id", required=True)
    dind_network.add_argument("--network-name", required=True)
    dind_network.add_argument("--container-id", required=True)
    dind_network.add_argument("--project", required=True)
    app_network = subparsers.add_parser("app-network")
    app_network.add_argument("--network-id", required=True)
    app_network.add_argument("--redis-container-id", required=True)
    app_network.add_argument("--influx-container-id", required=True)
    app_network.add_argument("--project", required=True)
    tool = subparsers.add_parser("tool-dockerfile")
    tool.add_argument("path")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "project":
            validate_project(args.value)
        elif args.command == "images":
            validate_image_ids(args.values)
        elif args.command == "new-evidence":
            validate_new_evidence_path(args.path, args.allowed_root, args.project)
        elif args.command == "private-directory":
            validate_created_private_directory(args.path)
        elif args.command == "compose":
            validate_compose_source(args.path)
        elif args.command == "recovery-scripts":
            validate_recovery_scripts(args.root)
        elif args.command == "dind-container":
            print(
                validate_dind_container(
                    sys.stdin.read(),
                    args.container_id,
                    args.image_id,
                    args.network_id,
                    args.network_name,
                    args.project,
                )
            )
        elif args.command == "dind-network":
            validate_dind_network(
                sys.stdin.read(),
                args.network_id,
                args.network_name,
                args.container_id,
                args.project,
            )
        elif args.command == "app-network":
            validate_app_network(
                sys.stdin.read(),
                args.network_id,
                args.redis_container_id,
                args.influx_container_id,
                args.project,
            )
        elif args.command == "tool-dockerfile":
            validate_recovery_tool_dockerfile(args.path)
        else:  # pragma: no cover - argparse makes this unreachable.
            raise SafetyError("unsupported safety check")
    except (OSError, SafetyError) as exc:
        print(f"SAFETY ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
