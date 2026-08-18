#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)"
COMPOSE_FILE="$SCRIPT_DIR/docker-compose.m0-ci.yml"
SAFETY="$SCRIPT_DIR/safety.py"

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

python3 "$SAFETY" project "$PROJECT"
python3 "$SAFETY" images \
  "$M0_BACKEND_IMAGE_ID" "$M0_WEB_IMAGE_ID" "$M0_REDIS_IMAGE_ID" \
  "$M0_INFLUX_IMAGE_ID" "$M0_TOOL_IMAGE_ID" "$M0_DIND_IMAGE_ID"
python3 "$SAFETY" compose "$COMPOSE_FILE"
python3 "$SAFETY" new-evidence --path "$EVIDENCE_ROOT" --allowed-root "$ALLOWED_ROOT" --project "$PROJECT"

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
STACK_STARTED=0
TOKEN_DEVICE=""
TOKEN_INODE=""

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
except OSError:
    pass
PY
}

cleanup() {
  set +e
  if test -n "$DIND_ID"; then
    docker rm -f "$DIND_ID" >/dev/null 2>&1
  fi
  if test -n "$DIND_NETWORK_ID"; then
    docker network rm "$DIND_NETWORK_ID" >/dev/null 2>&1
  fi
  if test "$STACK_STARTED" = "1"; then
    "${compose[@]}" --profile prep --profile tools down --volumes --remove-orphans >/dev/null 2>&1
  fi
  cleanup_token
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

install -d -m 0700 -- "$EVIDENCE_ROOT"
python3 "$SAFETY" private-directory "$EVIDENCE_ROOT"
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

"${compose[@]}" --profile tools run --rm --no-deps -T recovery-tool \
  python /repo/scripts/backup/sqlite_backup.py backup \
  --source /data/source.sqlite3 \
  --destination /evidence/sqlite-source.backup \
  > "$EVIDENCE_ROOT/sqlite-backup-report.json"
"${compose[@]}" --profile tools run --rm --no-deps -T recovery-tool \
  python /repo/scripts/backup/sqlite_backup.py restore \
  --source /evidence/sqlite-source.backup \
  --destination /data/restored.sqlite3 \
  > "$EVIDENCE_ROOT/sqlite-restore-report.json"

"${compose[@]}" --profile tools run --rm --no-deps -T recovery-tool \
  influx write --host http://influx:8086 --org m0-ci --bucket m0-source \
  --precision ns 'm0_sample,source=ci temperature=42.5 1704067200000000000'
"${compose[@]}" --profile tools run --rm --no-deps -T recovery-tool \
  python /repo/scripts/backup/influx_backup.py backup \
  --host http://influx:8086 --org m0-ci --bucket m0-source \
  --token-file /run/secrets/influx.token --path /evidence/influx-archive \
  > "$EVIDENCE_ROOT/influx-backup-report.json"
"${compose[@]}" --profile tools run --rm --no-deps -T recovery-tool \
  python /repo/scripts/backup/influx_backup.py verify-archive \
  --host http://influx:8086 --org m0-ci --bucket m0-source \
  --path /evidence/influx-archive \
  > "$EVIDENCE_ROOT/influx-verify-report.json"
"${compose[@]}" --profile tools run --rm --no-deps -T recovery-tool \
  python /repo/scripts/backup/influx_backup.py restore \
  --host http://influx:8086 --org m0-ci --bucket m0-source \
  --restore-bucket m0-restored --token-file /run/secrets/influx.token \
  --path /evidence/influx-archive \
  > "$EVIDENCE_ROOT/influx-restore-report.json"

flux_query='from(bucket: "m0-restored") |> range(start: time(v: 0)) |> filter(fn: (r) => r._measurement == "m0_sample" and r._field == "temperature") |> keep(columns: ["_value"])'
"${compose[@]}" --profile tools run --rm --no-deps -T recovery-tool \
  influx query --host http://influx:8086 --org m0-ci --raw "$flux_query" \
  > "$EVIDENCE_ROOT/influx-known-value.csv"
grep -Eq '(^|,)42\.5(,|$)' "$EVIDENCE_ROOT/influx-known-value.csv" || {
  echo "ERROR: restored Influx bucket does not contain the known value" >&2
  exit 2
}

python3 "$SCRIPT_DIR/image_archive.py" save \
  --path "$EVIDENCE_ROOT/rollback-images.tar" \
  --checksum "$EVIDENCE_ROOT/rollback-images.sha256.json" \
  --image-id "$M0_BACKEND_IMAGE_ID" \
  --image-id "$M0_WEB_IMAGE_ID" \
  --image-id "$M0_REDIS_IMAGE_ID" \
  > "$EVIDENCE_ROOT/image-save-report.json"

DIND_NETWORK_ID="$(docker network create --driver bridge --internal \
  --label "com.edge-iot.m0.project=$PROJECT" "$DIND_NETWORK_NAME")"
test -n "$DIND_NETWORK_ID"
DIND_ID="$(docker run -d --pull=never --privileged --name "$DIND_NAME" \
  --label "com.edge-iot.m0.project=$PROJECT" \
  --network "$DIND_NETWORK_ID" \
  --memory 768m --cpus 1.50 --pids-limit 512 \
  -p 127.0.0.1::2375 -e DOCKER_TLS_CERTDIR= \
  "$M0_DIND_IMAGE_ID" --host=tcp://0.0.0.0:2375 --tls=false)"
test -n "$DIND_ID"
DIND_PORT="$(docker port "$DIND_ID" 2375/tcp | awk -F: 'NR == 1 {print $NF}')"
test -n "$DIND_PORT"
DIND_HOST="tcp://127.0.0.1:$DIND_PORT"
for _attempt in $(seq 1 60); do
  if docker --host "$DIND_HOST" info >/dev/null 2>&1; then break; fi
  sleep 1
done
docker --host "$DIND_HOST" info >/dev/null
python3 "$SCRIPT_DIR/image_archive.py" load \
  --path "$EVIDENCE_ROOT/rollback-images.tar" \
  --checksum "$EVIDENCE_ROOT/rollback-images.sha256.json" \
  --docker-host "$DIND_HOST" \
  > "$EVIDENCE_ROOT/image-load-report.json"

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

python3 "$SCRIPT_DIR/stack_smoke.py" --base-url "http://127.0.0.1:$WEB_PORT" \
  > "$EVIDENCE_ROOT/stack-smoke-report.json"

check_worker() {
  local service="$1" node="$2" queue="$3"
  local ping_report="$EVIDENCE_ROOT/celery-${service}-ping.json"
  local queue_report="$EVIDENCE_ROOT/celery-${service}-queues.json"
  "${compose[@]}" exec -T "$service" celery -A control_plane inspect \
    --json --timeout 10 --destination "$node" ping > "$ping_report"
  "${compose[@]}" exec -T "$service" celery -A control_plane inspect \
    --json --timeout 10 --destination "$node" active_queues > "$queue_report"
  python3 "$SCRIPT_DIR/celery_smoke.py" \
    --ping-report "$ping_report" \
    --queue-report "$queue_report" \
    --node "$node" \
    --expected-queue "$queue"
}
check_worker celery-acq "m0-acq@${PROJECT}-acq" acquisition
check_worker celery-short "m0-short@${PROJECT}-short" short

python3 - "$EVIDENCE_ROOT" "$PROJECT" <<'PY'
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
(root / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
PY

# Artifact publication is allowed only after an exact byte scan proves that
# none of the three ephemeral credentials was copied into evidence.  The large
# rollback tar and its retained hard-link are excluded by inode.
python3 - "$EVIDENCE_ROOT" <<'PY'
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
secrets_to_find = [
    os.environ[name].encode("utf-8")
    for name in (
        "M0_INFLUX_TOKEN",
        "M0_DJANGO_SECRET_KEY",
        "M0_INFLUX_INIT_PASSWORD",
    )
]
archive = root / "rollback-images.tar"
archive_identity = None
if archive.exists():
    archive_stat = os.lstat(archive)
    archive_identity = (archive_stat.st_dev, archive_stat.st_ino)
maximum = max(len(value) for value in secrets_to_find)
for path in root.rglob("*"):
    metadata = os.lstat(path)
    if stat.S_ISDIR(metadata.st_mode):
        continue
    if not stat.S_ISREG(metadata.st_mode):
        raise SystemExit("evidence contains a non-regular entry; refusing artifact")
    if archive_identity == (metadata.st_dev, metadata.st_ino):
        continue
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise SystemExit("evidence entry changed during credential scan")
        initial_size = current.st_size
        initial_mtime_ns = current.st_mtime_ns
        carry = b""
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            candidate = carry + block
            if any(secret in candidate for secret in secrets_to_find):
                raise SystemExit("ephemeral credential found in evidence; refusing artifact")
            carry = candidate[-(maximum - 1):] if maximum > 1 else b""
        final = os.fstat(descriptor)
        final_path = os.lstat(path)
        if (
            (final.st_dev, final.st_ino) != (metadata.st_dev, metadata.st_ino)
            or (final_path.st_dev, final_path.st_ino)
            != (metadata.st_dev, metadata.st_ino)
            or final.st_size != initial_size
            or final.st_mtime_ns != initial_mtime_ns
        ):
            raise SystemExit("evidence entry changed during credential scan")
    finally:
        os.close(descriptor)
PY

echo "M0 isolated recovery drill passed; evidence: $EVIDENCE_ROOT"
