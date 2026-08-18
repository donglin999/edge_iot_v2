#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.m0-ci.yml"
SAFETY="$SCRIPT_DIR/safety.py"
EVIDENCE_IO="$SCRIPT_DIR/evidence_io.py"

usage() {
  echo "usage: $0 --project m0ci-<unique> --evidence <new-path> --allowed-root <private-root>" >&2
}

PROJECT=""
EVIDENCE_ROOT=""
ALLOWED_ROOT=""
while (($#)); do
  case "$1" in
    --project) PROJECT="${2:-}"; shift 2 ;;
    --evidence) EVIDENCE_ROOT="${2:-}"; shift 2 ;;
    --allowed-root) ALLOWED_ROOT="${2:-}"; shift 2 ;;
    *) usage; exit 2 ;;
  esac
done

test -n "$PROJECT" && test -n "$EVIDENCE_ROOT" && test -n "$ALLOWED_ROOT" || { usage; exit 2; }
test "${M0_IMAGES_PREPARED:-}" = "1" || {
  echo "SAFETY ERROR: explicit image preparation acknowledgement M0_IMAGES_PREPARED=1 is required" >&2
  exit 2
}

required_variables=(
  M0_BACKEND_IMAGE_ID M0_WEB_IMAGE_ID M0_REDIS_IMAGE_ID
  M0_INFLUX_IMAGE_ID M0_TOOL_IMAGE_ID M0_DIND_IMAGE_ID
)
for variable in "${required_variables[@]}"; do
  test -n "${!variable:-}" || { echo "SAFETY ERROR: $variable is required" >&2; exit 2; }
done

python3 "$SAFETY" recovery-scripts --root "$SCRIPT_DIR"
python3 "$SAFETY" project "$PROJECT"
python3 "$SAFETY" images \
  "$M0_BACKEND_IMAGE_ID" "$M0_WEB_IMAGE_ID" "$M0_REDIS_IMAGE_ID" \
  "$M0_INFLUX_IMAGE_ID" "$M0_TOOL_IMAGE_ID" "$M0_DIND_IMAGE_ID"
python3 "$SAFETY" compose "$COMPOSE_FILE"
if test "${M0_EVIDENCE_READY:-}" != "1"; then
  exec python3 "$EVIDENCE_IO" create-and-exec \
    --path "$EVIDENCE_ROOT" \
    --allowed-root "$ALLOWED_ROOT" \
    --project "$PROJECT" \
    --script "$0"
fi
test "${M0_EVIDENCE_GUARD_PID:-}" = "$$" || {
  echo "SAFETY ERROR: evidence guard was not created by this drill process" >&2
  exit 2
}
EVIDENCE_FD="${M0_EVIDENCE_DIR_FD:?held evidence directory fd required}"
EVIDENCE_DEVICE="${M0_EVIDENCE_DEVICE:?evidence device required}"
EVIDENCE_INODE="${M0_EVIDENCE_INODE:?evidence inode required}"
python3 "$EVIDENCE_IO" verify \
  --root "$EVIDENCE_ROOT" --fd "$EVIDENCE_FD" \
  --device "$EVIDENCE_DEVICE" --inode "$EVIDENCE_INODE"

for image_id in \
  "$M0_BACKEND_IMAGE_ID" "$M0_WEB_IMAGE_ID" "$M0_REDIS_IMAGE_ID" \
  "$M0_INFLUX_IMAGE_ID" "$M0_TOOL_IMAGE_ID" "$M0_DIND_IMAGE_ID"; do
  actual_id="$(docker image inspect "$image_id" --format '{{.Id}}')"
  test "$actual_id" = "$image_id" || { echo "SAFETY ERROR: prepared image identity mismatch" >&2; exit 2; }
done

if test -n "$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT")"; then
  echo "SAFETY ERROR: disposable Compose project already has containers" >&2
  exit 2
fi
if test -n "$(docker volume ls -q --filter "label=com.docker.compose.project=$PROJECT")"; then
  echo "SAFETY ERROR: disposable Compose project already has volumes" >&2
  exit 2
fi
if test -n "$(docker network ls -q --filter "label=com.docker.compose.project=$PROJECT")"; then
  echo "SAFETY ERROR: disposable Compose project already has networks" >&2
  exit 2
fi
DIND_NAME="${PROJECT}-dind"
if docker container inspect "$DIND_NAME" >/dev/null 2>&1; then
  echo "SAFETY ERROR: disposable DinD name already exists" >&2
  exit 2
fi
DIND_NETWORK_NAME="${PROJECT}-dind-net"
if docker network inspect "$DIND_NETWORK_NAME" >/dev/null 2>&1; then
  echo "SAFETY ERROR: disposable DinD network already exists" >&2
  exit 2
fi
M0_TOKEN_FILE="$ALLOWED_ROOT/.${PROJECT}.influx.token"
if test -e "$M0_TOKEN_FILE" || test -L "$M0_TOKEN_FILE"; then
  echo "SAFETY ERROR: disposable token path already exists" >&2
  exit 2
fi

export M0_UID="$(id -u)" M0_GID="$(id -g)"
export M0_REPO_ROOT="$REPO_ROOT" M0_EVIDENCE_ROOT="$EVIDENCE_ROOT" M0_TOKEN_FILE
export COMPOSE_PROJECT_NAME="$PROJECT"
compose=(docker compose -p "$PROJECT" -f "$COMPOSE_FILE")
DIND_ID=""
DIND_NETWORK_ID=""
DIND_VOLUME_NAME=""
STACK_STARTED=0
TOKEN_DEVICE=""
TOKEN_INODE=""

run_evidence() {
  local name="$1"
  shift
  python3 "$EVIDENCE_IO" capture \
    --root "$EVIDENCE_ROOT" --fd "$EVIDENCE_FD" \
    --device "$EVIDENCE_DEVICE" --inode "$EVIDENCE_INODE" \
    --name "$name" -- "$@"
}

cleanup_token() {
  python3 - "$M0_TOKEN_FILE" "$TOKEN_DEVICE" "$TOKEN_INODE" <<'PY'
import os
import stat
import sys

path, expected_device, expected_inode = sys.argv[1:]
if not expected_device or not expected_inode:
    raise SystemExit(0)
try:
    metadata = os.lstat(path)
    if (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_dev == int(expected_device)
        and metadata.st_ino == int(expected_inode)
    ):
        os.unlink(path)
    else:
        raise SystemExit("token path identity changed; refusing path cleanup")
except FileNotFoundError:
    raise SystemExit(0)
if os.path.lexists(path):
    raise SystemExit("token path survived cleanup")
PY
}

cleanup() {
  local original_status=$?
  local cleanup_failed=0
  local remaining=""
  local compose_containers=""
  local compose_volumes=""
  local compose_networks=""
  set +e
  if test -n "$DIND_ID"; then
    # docker:dind declares /var/lib/docker as a VOLUME.  Removing the
    # container with -v is required to remove that project-private anonymous
    # volume and the rollback images it contains.
    docker rm -f -v "$DIND_ID" >/dev/null 2>&1 || cleanup_failed=1
    remaining="$(docker container ls -aq --no-trunc)" || cleanup_failed=1
    if printf '%s\n' "$remaining" | grep -Fqx -- "$DIND_ID"; then
      echo "SAFETY ERROR: disposable DinD container survived cleanup" >&2
      cleanup_failed=1
    fi
  fi
  if test -n "$DIND_VOLUME_NAME"; then
    remaining="$(docker volume ls -q)" || cleanup_failed=1
    if printf '%s\n' "$remaining" | grep -Fqx -- "$DIND_VOLUME_NAME"; then
      echo "SAFETY ERROR: disposable DinD data volume survived cleanup" >&2
      cleanup_failed=1
    fi
  fi
  if test -n "$DIND_NETWORK_ID"; then
    docker network rm "$DIND_NETWORK_ID" >/dev/null 2>&1 || cleanup_failed=1
    remaining="$(docker network ls -q --no-trunc)" || cleanup_failed=1
    if printf '%s\n' "$remaining" | grep -Fqx -- "$DIND_NETWORK_ID"; then
      echo "SAFETY ERROR: disposable DinD network survived cleanup" >&2
      cleanup_failed=1
    fi
  fi
  if test "$STACK_STARTED" = "1"; then
    "${compose[@]}" --profile prep --profile tools down --volumes --remove-orphans >/dev/null 2>&1 || cleanup_failed=1
    compose_containers="$(docker ps -aq --no-trunc --filter "label=com.docker.compose.project=$PROJECT")" || cleanup_failed=1
    compose_volumes="$(docker volume ls -q --filter "label=com.docker.compose.project=$PROJECT")" || cleanup_failed=1
    compose_networks="$(docker network ls -q --no-trunc --filter "label=com.docker.compose.project=$PROJECT")" || cleanup_failed=1
    if test -n "$compose_containers" || test -n "$compose_volumes" || test -n "$compose_networks"; then
      echo "SAFETY ERROR: disposable Compose resources survived cleanup" >&2
      cleanup_failed=1
    fi
  fi
  cleanup_token || cleanup_failed=1
  if test "$cleanup_failed" = "1"; then
    original_status=1
  fi
  trap - EXIT
  exit "$original_status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

TOKEN_IDENTITY="$(python3 - "$M0_TOKEN_FILE" <<'PY'
import os
import secrets
import sys

path = sys.argv[1]
descriptor = None
try:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    payload = (secrets.token_urlsafe(48) + "\n").encode()
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("could not completely write ephemeral token")
        offset += written
    os.fsync(descriptor)
    metadata = os.fstat(descriptor)
except Exception:
    if descriptor is not None:
        metadata = os.fstat(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            current = os.lstat(path)
            if (current.st_dev, current.st_ino) == (metadata.st_dev, metadata.st_ino):
                os.unlink(path)
        except OSError:
            pass
    raise
finally:
    if descriptor is not None:
        os.close(descriptor)
print(metadata.st_dev, metadata.st_ino)
PY
)"
read -r TOKEN_DEVICE TOKEN_INODE <<< "$TOKEN_IDENTITY"
M0_INFLUX_TOKEN="$(python3 - "$M0_TOKEN_FILE" "$TOKEN_DEVICE" "$TOKEN_INODE" <<'PY'
import os
import stat
import sys

path, expected_device, expected_inode = sys.argv[1:]
descriptor = os.open(
    path,
    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
)
try:
    metadata = os.fstat(descriptor)
    current = os.lstat(path)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or (metadata.st_dev, metadata.st_ino)
        != (int(expected_device), int(expected_inode))
        or (metadata.st_dev, metadata.st_ino)
        != (current.st_dev, current.st_ino)
    ):
        raise SystemExit("token identity or mode changed")
    token = os.read(descriptor, 65537)
finally:
    os.close(descriptor)
if len(token) > 65536 or len(token.splitlines()) != 1:
    raise SystemExit("token content is invalid")
print(token.decode("utf-8").strip())
PY
)"
test -n "$M0_INFLUX_TOKEN"
M0_DJANGO_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(64))')"
M0_INFLUX_INIT_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
test -n "$M0_DJANGO_SECRET_KEY" && test -n "$M0_INFLUX_INIT_PASSWORD"

export M0_INFLUX_TOKEN M0_DJANGO_SECRET_KEY M0_INFLUX_INIT_PASSWORD
export COMPOSE_PROJECT_NAME="$PROJECT"
"${compose[@]}" config --quiet

STACK_STARTED=1
"${compose[@]}" up -d --wait redis influx
"${compose[@]}" --profile prep run --rm --no-deps volume-init
"${compose[@]}" --profile prep run --rm --no-deps \
  -e DJANGO_DB_NAME=/data/source.sqlite3 migrate
"${compose[@]}" --profile prep run --rm --no-deps \
  -e DJANGO_DB_NAME=/data/source.sqlite3 seed

run_evidence sqlite-backup-report.json \
  "${compose[@]}" --profile tools run --rm --no-deps -T recovery-tool \
  python /repo/scripts/backup/sqlite_backup.py backup \
  --source /data/source.sqlite3 \
  --destination /evidence/sqlite-source.backup
run_evidence sqlite-restore-report.json \
  "${compose[@]}" --profile tools run --rm --no-deps -T recovery-tool \
  python /repo/scripts/backup/sqlite_backup.py restore \
  --source /evidence/sqlite-source.backup \
  --destination /data/restored.sqlite3

"${compose[@]}" --profile tools run --rm --no-deps -T recovery-tool \
  influx write --host http://influx:8086 --org m0-ci --bucket m0-source \
  --precision ns 'm0_sample,source=ci temperature=42.5 1704067200000000000'
run_evidence influx-backup-report.json \
  "${compose[@]}" --profile tools run --rm --no-deps -T recovery-tool \
  python /repo/scripts/backup/influx_backup.py backup \
  --host http://influx:8086 --org m0-ci --bucket m0-source \
  --token-file /run/secrets/influx.token --path /evidence/influx-archive
run_evidence influx-verify-report.json \
  "${compose[@]}" --profile tools run --rm --no-deps -T recovery-tool \
  python /repo/scripts/backup/influx_backup.py verify-archive \
  --host http://influx:8086 --org m0-ci --bucket m0-source \
  --path /evidence/influx-archive
run_evidence influx-restore-report.json \
  "${compose[@]}" --profile tools run --rm --no-deps -T recovery-tool \
  python /repo/scripts/backup/influx_backup.py restore \
  --host http://influx:8086 --org m0-ci --bucket m0-source \
  --restore-bucket m0-restored --token-file /run/secrets/influx.token \
  --path /evidence/influx-archive

flux_query='from(bucket: "m0-restored") |> range(start: time(v: 0)) |> filter(fn: (r) => r._measurement == "m0_sample" and r._field == "temperature") |> keep(columns: ["_value"])'
run_evidence influx-known-value.csv \
  "${compose[@]}" --profile tools run --rm --no-deps -T recovery-tool \
  influx query --host http://influx:8086 --org m0-ci --raw "$flux_query"
run_evidence influx-known-value-report.json \
  python3 "$SCRIPT_DIR/influx_value_check.py" \
  --path "$EVIDENCE_ROOT/influx-known-value.csv" --expected 42.5

run_evidence image-save-report.json python3 "$SCRIPT_DIR/image_archive.py" save \
  --path "$EVIDENCE_ROOT/rollback-images.tar" \
  --checksum "$EVIDENCE_ROOT/rollback-images.sha256.json" \
  --image-id "$M0_BACKEND_IMAGE_ID" \
  --image-id "$M0_WEB_IMAGE_ID" \
  --image-id "$M0_REDIS_IMAGE_ID"

DIND_NETWORK_ID="$(docker network create --driver bridge --internal \
  --label "com.edge-iot.m0.project=$PROJECT" "$DIND_NETWORK_NAME")"
test -n "$DIND_NETWORK_ID"
DIND_ID="$(docker run -d --pull=never --privileged --name "$DIND_NAME" \
  --label "com.edge-iot.m0.project=$PROJECT" \
  --network "$DIND_NETWORK_ID" \
  --memory 768m --cpus 1.50 --pids-limit 512 \
  -e DOCKER_TLS_CERTDIR= \
  "$M0_DIND_IMAGE_ID" \
  dockerd --host=unix:///var/run/docker.sock)"
test -n "$DIND_ID"
DIND_VOLUME_NAME="$(docker container inspect "$DIND_ID" | python3 "$SAFETY" dind-container \
  --container-id "$DIND_ID" --image-id "$M0_DIND_IMAGE_ID" \
  --network-id "$DIND_NETWORK_ID" --network-name "$DIND_NETWORK_NAME" \
  --project "$PROJECT")"
test -n "$DIND_VOLUME_NAME"
docker network inspect "$DIND_NETWORK_ID" | python3 "$SAFETY" dind-network \
  --network-id "$DIND_NETWORK_ID" --network-name "$DIND_NETWORK_NAME" \
  --container-id "$DIND_ID" --project "$PROJECT"
for _attempt in $(seq 1 60); do
  if docker exec "$DIND_ID" docker info >/dev/null 2>&1; then break; fi
  sleep 1
done
docker exec "$DIND_ID" docker info >/dev/null
run_evidence image-load-report.json python3 "$SCRIPT_DIR/image_archive.py" load \
  --path "$EVIDENCE_ROOT/rollback-images.tar" \
  --checksum "$EVIDENCE_ROOT/rollback-images.sha256.json" \
  --docker-container "$DIND_ID"

"${compose[@]}" up -d --wait django celery-acq celery-short web
for service in redis influx django celery-acq celery-short web; do
  container_id="$("${compose[@]}" ps -q "$service")"
  test -n "$container_id"
  case "$service" in
    redis) expected="$M0_REDIS_IMAGE_ID" ;;
    influx) expected="$M0_INFLUX_IMAGE_ID" ;;
    web) expected="$M0_WEB_IMAGE_ID" ;;
    *) expected="$M0_BACKEND_IMAGE_ID" ;;
  esac
  actual="$(docker inspect "$container_id" --format '{{.Image}}')"
  test "$actual" = "$expected" || { echo "ERROR: $service is not SHA-pinned" >&2; exit 2; }
done

WEB_PORT="$("${compose[@]}" port web 80 | awk -F: 'NR == 1 {print $NF}')"
test -n "$WEB_PORT"
WEB_CONTAINER="$("${compose[@]}" ps -q web)"
INFLUX_CONTAINER="$("${compose[@]}" ps -q influx)"
python3 - "$WEB_CONTAINER" "$INFLUX_CONTAINER" <<'PY'
import json
import subprocess
import sys

for container in sys.argv[1:]:
    ports = json.loads(subprocess.check_output([
        "docker", "inspect", container,
        "--format", "{{json .NetworkSettings.Ports}}",
    ]))
    seen = 0
    for bindings in ports.values():
        for binding in bindings or []:
            seen += 1
            if binding["HostIp"] != "127.0.0.1":
                raise SystemExit("published drill port is not loopback-bound")
    if seen != 1:
        raise SystemExit("each published drill service must have one loopback binding")
PY

run_evidence stack-smoke-report.json \
  python3 "$SCRIPT_DIR/stack_smoke.py" --base-url "http://127.0.0.1:$WEB_PORT"

check_worker() {
  local service="$1" node="$2" queue="$3"
  local ping_report="$EVIDENCE_ROOT/celery-${service}-ping.json"
  local queue_report="$EVIDENCE_ROOT/celery-${service}-queues.json"
  run_evidence "celery-${service}-ping.json" \
    "${compose[@]}" exec -T "$service" celery -A control_plane inspect \
    --json --timeout 10 --destination "$node" ping
  run_evidence "celery-${service}-queues.json" \
    "${compose[@]}" exec -T "$service" celery -A control_plane inspect \
    --json --timeout 10 --destination "$node" active_queues
  python3 "$SCRIPT_DIR/celery_smoke.py" \
    --ping-report "$ping_report" \
    --queue-report "$queue_report" \
    --node "$node" \
    --expected-queue "$queue"
}
check_worker celery-acq "m0-acq@${PROJECT}-acq" acquisition
check_worker celery-short "m0-short@${PROJECT}-short" short

run_evidence summary.json python3 - "$EVIDENCE_ROOT" "$PROJECT" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1])
required = (
    "sqlite-backup-report.json",
    "sqlite-restore-report.json",
    "influx-backup-report.json",
    "influx-verify-report.json",
    "influx-restore-report.json",
    "influx-known-value-report.json",
    "image-save-report.json",
    "image-load-report.json",
    "stack-smoke-report.json",
    "celery-celery-acq-ping.json",
    "celery-celery-acq-queues.json",
    "celery-celery-short-ping.json",
    "celery-celery-short-queues.json",
)
for name in required:
    if not (root / name).is_file() or (root / name).stat().st_size == 0:
        raise SystemExit(f"missing evidence: {name}")
summary = {
    "operation": "m0-isolated-recovery-drill",
    "project": sys.argv[2],
    "completed_at": datetime.now(timezone.utc).isoformat(),
    "sqlite_backup_restore": "ok",
    "influx_backup_verify_restore_known_value": "ok",
    "held_fd_image_archive_independent_dind_load": "ok",
    "sha_pinned_rollback_stack": "ok",
    "restored_static_fact_http_api_websocket_alarm_data": "ok",
    "celery_nodes_and_active_queues": "ok",
}
print(json.dumps(summary, indent=2, sort_keys=True))
PY

# Evidence remains runner-local and is never handed to an artifact uploader.
# Recheck its held directory identity before reporting the CI gate result.
python3 "$EVIDENCE_IO" verify \
  --root "$EVIDENCE_ROOT" --fd "$EVIDENCE_FD" \
  --device "$EVIDENCE_DEVICE" --inode "$EVIDENCE_INODE"

echo "M0 isolated recovery drill passed; runner-local evidence was not uploaded"
