#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."

VALIDATOR="$PWD/scripts/offline/validate-env.sh"
TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/edge-iot-env-test.XXXXXX")
trap 'rm -rf "$TMP_DIR"' EXIT

SECRET_KEY_VALUE=''
for _ in {1..32}; do SECRET_KEY_VALUE="${SECRET_KEY_VALUE}a1"; done
INFLUX_TOKEN_VALUE=$(printf 'influx-%058d' 987654321)

write_valid_env() {
  local path=$1
  {
    echo 'DEBUG=False'
    printf 'SECRET_KEY=%s\n' "$SECRET_KEY_VALUE"
    echo 'ALLOWED_HOSTS=127.0.0.1,edge.local'
    echo 'ALLOW_PRIVATE_NETWORK_TESTS=False'
    echo 'WEB_PORT=80'
    echo 'CELERY_ACQ_CONCURRENCY=32'
    echo 'CELERY_SHORT_CONCURRENCY=8'
    echo 'REDIS_HOST=127.0.0.1'
    echo 'REDIS_PORT=6380'
    echo 'LEGACY_CONTAINERS=edge_iot'
    echo 'INFLUXDB_HOST=127.0.0.1'
    echo 'INFLUXDB_PORT=8086'
    echo 'INFLUXDB_ORG=plant-org'
    echo 'INFLUXDB_BUCKET=edge-records'
    printf 'INFLUXDB_TOKEN=%s\n' "$INFLUX_TOKEN_VALUE"
  } > "$path"
  chmod 600 "$path"
}

expect_fail() {
  local label=$1 file=$2 expected=$3 output="$TMP_DIR/output"
  if "$VALIDATOR" "$file" >"$output" 2>&1; then
    echo "FAIL: $label unexpectedly passed" >&2
    exit 1
  fi
  grep -Fq "$expected" "$output" || {
    echo "FAIL: $label did not report expected reason: $expected" >&2
    sed -n '1,20p' "$output" >&2
    exit 1
  }
  if grep -Fq "$SECRET_KEY_VALUE" "$output" || grep -Fq "$INFLUX_TOKEN_VALUE" "$output"; then
    echo "FAIL: $label leaked a credential into output" >&2
    exit 1
  fi
}

VALID="$TMP_DIR/valid.env"
write_valid_env "$VALID"
"$VALIDATOR" "$VALID" >/dev/null

INCOMPLETE="$TMP_DIR/incomplete.env"
cp deploy/offline/.env.example "$INCOMPLETE"
chmod 600 "$INCOMPLETE"
expect_fail "incomplete example" "$INCOMPLETE" "SECRET_KEY 不能为空"

WEAK_MODE="$TMP_DIR/weak-mode.env"
write_valid_env "$WEAK_MODE"
chmod 644 "$WEAK_MODE"
expect_fail "weak file mode" "$WEAK_MODE" "0400 或 0600"

WILDCARD="$TMP_DIR/wildcard.env"
write_valid_env "$WILDCARD"
sed -i.bak 's/^ALLOWED_HOSTS=.*/ALLOWED_HOSTS=*/' "$WILDCARD"
expect_fail "wildcard hosts" "$WILDCARD" "禁止使用通配符"

DEBUG_TRUE="$TMP_DIR/debug.env"
write_valid_env "$DEBUG_TRUE"
sed -i.bak 's/^DEBUG=False/DEBUG=True/' "$DEBUG_TRUE"
expect_fail "debug enabled" "$DEBUG_TRUE" "DEBUG 在离线生产包中必须为 False"

PRIVATE_NO_ACK="$TMP_DIR/private-no-ack.env"
write_valid_env "$PRIVATE_NO_ACK"
sed -i.bak 's/^ALLOW_PRIVATE_NETWORK_TESTS=False/ALLOW_PRIVATE_NETWORK_TESTS=True/' "$PRIVATE_NO_ACK"
expect_fail "private test without acknowledgement" "$PRIVATE_NO_ACK" "必须显式确认 SSRF 风险"

PRIVATE_ACK="$TMP_DIR/private-ack.env"
cp "$PRIVATE_NO_ACK" "$PRIVATE_ACK"
echo 'OFFLINE_PRIVATE_NETWORK_TESTS_ACK=I_UNDERSTAND_PRIVATE_NETWORK_TESTS_DISABLE_SSRF_PROTECTION' >> "$PRIVATE_ACK"
chmod 400 "$PRIVATE_ACK"
"$VALIDATOR" "$PRIVATE_ACK" >/dev/null

PLACEHOLDER="$TMP_DIR/placeholder.env"
write_valid_env "$PLACEHOLDER"
sed -i.bak 's/^SECRET_KEY=.*/SECRET_KEY=replace-me-1234567890123456789012345678901234567890/' "$PLACEHOLDER"
expect_fail "placeholder secret" "$PLACEHOLDER" "SECRET_KEY 仍是占位值"

SYMLINK="$TMP_DIR/link.env"
ln -s "$VALID" "$SYMLINK"
expect_fail "symlink" "$SYMLINK" "不能是软链接"

COMPOSE_CONTROL="$TMP_DIR/compose-control.env"
cp "$VALID" "$COMPOSE_CONTROL"
echo 'COMPOSE_PROJECT_NAME=caller-controlled' >> "$COMPOSE_CONTROL"
expect_fail "Compose control key" "$COMPOSE_CONTROL" "禁止定义 COMPOSE_*"

BAD_LEGACY="$TMP_DIR/bad-legacy.env"
write_valid_env "$BAD_LEGACY"
sed -i.bak 's/^LEGACY_CONTAINERS=.*/LEGACY_CONTAINERS=--all/' "$BAD_LEGACY"
expect_fail "unsafe legacy container" "$BAD_LEGACY" "包含非法容器名"

NO_LEGACY="$TMP_DIR/no-legacy.env"
write_valid_env "$NO_LEGACY"
sed -i.bak 's/^LEGACY_CONTAINERS=.*/LEGACY_CONTAINERS=/' "$NO_LEGACY"
"$VALIDATOR" "$NO_LEGACY" >/dev/null

INTERPOLATED="$TMP_DIR/interpolated.env"
write_valid_env "$INTERPOLATED"
sed -i.bak 's/^ALLOWED_HOSTS=.*/ALLOWED_HOSTS=${HOME}/' "$INTERPOLATED"
expect_fail "Compose env interpolation" "$INTERPOLATED" "不支持的语法"

EXPORTED="$TMP_DIR/exported.env"
write_valid_env "$EXPORTED"
echo 'export ALLOWED_HOSTS=caller.invalid' >> "$EXPORTED"
expect_fail "alternate export syntax" "$EXPORTED" "不支持的语法"

# Pinning creates a private copy and detects both in-place changes and inode replacement.
PIN_DIR="$TMP_DIR/pin"
mkdir "$PIN_DIR"
chmod 700 "$PIN_DIR"
PINNED_ENV="$PIN_DIR/runtime.env"
RECEIPT=$(python3 -I scripts/offline/prepare-env.py prepare "$VALID" "$PINNED_ENV")
python3 -I scripts/offline/prepare-env.py verify "$PINNED_ENV" "$RECEIPT"
echo '# changed after validation' >> "$PINNED_ENV"
if python3 -I scripts/offline/prepare-env.py verify "$PINNED_ENV" "$RECEIPT" \
    >"$TMP_DIR/pin-modified" 2>&1; then
  echo "FAIL: in-place env modification was not detected" >&2
  exit 1
fi
grep -Fq 'identity or content changed' "$TMP_DIR/pin-modified"

PIN_DIR_2="$TMP_DIR/pin-replaced"
mkdir "$PIN_DIR_2"
chmod 700 "$PIN_DIR_2"
PINNED_ENV_2="$PIN_DIR_2/runtime.env"
RECEIPT_2=$(python3 -I scripts/offline/prepare-env.py prepare "$VALID" "$PINNED_ENV_2")
cp "$PINNED_ENV_2" "$PINNED_ENV_2.new"
chmod 600 "$PINNED_ENV_2.new"
rm "$PINNED_ENV_2"
mv "$PINNED_ENV_2.new" "$PINNED_ENV_2"
if python3 -I scripts/offline/prepare-env.py verify "$PINNED_ENV_2" "$RECEIPT_2" \
    >"$TMP_DIR/pin-replaced-output" 2>&1; then
  echo "FAIL: env inode replacement was not detected" >&2
  exit 1
fi
grep -Fq 'identity or content changed' "$TMP_DIR/pin-replaced-output"

# Build a minimal but structurally valid offline bundle so the loader reaches its
# Compose preflight without loading or removing a real Docker object.
BUNDLE="$TMP_DIR/bundle"
mkdir "$BUNDLE"
cp scripts/offline/load-and-up.sh scripts/offline/validate-env.sh \
   scripts/offline/prepare-env.py scripts/offline/verify-image-archive.py "$BUNDLE/"
cp deploy/offline/docker-compose.offline.yml "$BUNDLE/docker-compose.yml"
chmod +x "$BUNDLE"/*.sh "$BUNDLE"/*.py
python3 -I - "$BUNDLE" <<'PY'
import hashlib
import io
import json
import pathlib
import sys
import tarfile

root = pathlib.Path(sys.argv[1])
images = [
    ("edge-iot/backend:offline-amd64", "backend.json"),
    ("edge-iot/web:offline-amd64", "web.json"),
    ("redis:7.0.10", "redis.json"),
]
manifest = [{"Config": config, "RepoTags": [tag], "Layers": []} for tag, config in images]
payloads = {config: b'{"architecture":"amd64","os":"linux"}' for _, config in images}
payloads["manifest.json"] = json.dumps(manifest, separators=(",", ":")).encode()
archive_path = root / "images.tar"
with tarfile.open(archive_path, "w") as archive:
    for name, payload in payloads.items():
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
(root / "images.tar.sha256").write_text(f"{digest}  images.tar\n", encoding="ascii")
PY

# Caller shell values have higher native Compose precedence than --env-file.
# The loader must remove every such value before even `compose config`.
FAKE_BIN="$TMP_DIR/bin"
DOCKER_LOG="$TMP_DIR/docker-log"
ENV_LEAK_MARKER="$TMP_DIR/caller-env-leaked"
mkdir "$FAKE_BIN"
{
  echo '#!/usr/bin/env bash'
  printf 'log=%q\n' "$DOCKER_LOG"
  printf 'leak=%q\n' "$ENV_LEAK_MARKER"
  echo 'printf "%s\n" "$*" >> "$log"'
  echo 'sensitive=0'
  echo 'for arg in "$@"; do case "$arg" in config|up|ps) sensitive=1 ;; esac; done'
  echo 'if [ "$sensitive" -eq 1 ] && {'
  echo '   [ "${SECRET_KEY+x}" = x ] || [ "${INFLUXDB_TOKEN+x}" = x ] ||'
  echo '   [ "${DEBUG+x}" = x ] || [ "${ALLOWED_HOSTS+x}" = x ] ||'
  echo '   [ "${INFLUXDB_HOST+x}" = x ] || [ "${COMPOSE_PROJECT_NAME+x}" = x ] ||'
  echo '   [ "${COMPOSE_ANSI+x}" = x ] || [ "${LEGACY_CONTAINERS+x}" = x ]; }; then'
  echo '  touch "$leak"'
  echo 'fi'
  echo '[ "${1:-}" = compose ] && [ "${2:-}" = version ] && exit 0'
  echo 'for arg in "$@"; do [ "$arg" = config ] && exit 0; done'
  echo '[ "${1:-}" = load ] && exit 99'
  echo 'exit 99'
} > "$FAKE_BIN/docker"
{
  echo '#!/usr/bin/env bash'
  echo 'exit 0'
} > "$FAKE_BIN/curl"
chmod +x "$FAKE_BIN/docker"
chmod +x "$FAKE_BIN/curl"
LOADER_OUTPUT="$TMP_DIR/loader-output"
if SECRET_KEY=caller-secret INFLUXDB_TOKEN=caller-token DEBUG=True \
    ALLOWED_HOSTS='*' INFLUXDB_HOST=caller.invalid LEGACY_CONTAINERS=caller-legacy \
    COMPOSE_PROJECT_NAME=caller COMPOSE_ANSI=always \
    PATH="$FAKE_BIN:$PATH" "$BUNDLE/load-and-up.sh" --env-file "$VALID" \
      >"$LOADER_OUTPUT" 2>&1; then
  echo "FAIL: fake docker load should have stopped the loader" >&2
  exit 1
fi
[ ! -e "$ENV_LEAK_MARKER" ] || {
  echo "FAIL: caller shell configuration reached Compose/Docker" >&2
  exit 1
}
grep -Fq 'compose version' "$DOCKER_LOG"
grep -Fq 'config --quiet' "$DOCKER_LOG"
grep -Fq 'load -i images.tar' "$DOCKER_LOG"
if grep -Fq 'caller-secret' "$LOADER_OUTPUT" || grep -Fq 'caller-token' "$LOADER_OUTPUT"; then
  echo "FAIL: caller credential appeared in loader output" >&2
  exit 1
fi

NO_SUM_BUNDLE="$TMP_DIR/no-checksum-bundle"
cp -R "$BUNDLE" "$NO_SUM_BUNDLE"
rm "$NO_SUM_BUNDLE/images.tar.sha256"
: > "$DOCKER_LOG"
if PATH="$FAKE_BIN:$PATH" "$NO_SUM_BUNDLE/load-and-up.sh" --env-file "$VALID" \
    >"$TMP_DIR/no-checksum-output" 2>&1; then
  echo "FAIL: loader accepted a bundle without images.tar.sha256" >&2
  exit 1
fi
grep -Fq 'images.tar.sha256' "$TMP_DIR/no-checksum-output"
if grep -Eq '(^| )load -i|(^| )rm -f|(^| )stop( |$)' "$DOCKER_LOG"; then
  echo "FAIL: loader mutated Docker state without a required checksum" >&2
  exit 1
fi

# A validator replacement attack against the pinned file must fail before Docker.
RACE_BUNDLE="$TMP_DIR/race-bundle"
cp -R "$BUNDLE" "$RACE_BUNDLE"
{
  echo '#!/usr/bin/env bash'
  echo 'set -euo pipefail'
  echo 'replacement="$1.replacement"'
  echo 'cp "$1" "$replacement"'
  echo 'rm "$1"'
  echo 'mv "$replacement" "$1"'
  echo 'chmod 600 "$1"'
} > "$RACE_BUNDLE/validate-env.sh"
chmod +x "$RACE_BUNDLE/validate-env.sh"
: > "$DOCKER_LOG"
if PATH="$FAKE_BIN:$PATH" "$RACE_BUNDLE/load-and-up.sh" --env-file "$VALID" \
    >"$TMP_DIR/race-output" 2>&1; then
  echo "FAIL: validator inode replacement unexpectedly passed" >&2
  exit 1
fi
grep -Fq 'identity or content changed' "$TMP_DIR/race-output"
[ ! -s "$DOCKER_LOG" ] || {
  echo "FAIL: Docker was called after env snapshot replacement" >&2
  exit 1
}

# Exercise the real Compose interpolator when available: missing values fail,
# while a validated env renders quietly without exposing credentials.
if docker compose version >/dev/null 2>&1; then
  if docker compose -f deploy/offline/docker-compose.offline.yml config --quiet \
      >"$TMP_DIR/compose-missing" 2>&1; then
    echo "FAIL: Compose rendered without required production values" >&2
    exit 1
  fi
  grep -Fq 'SECRET_KEY' "$TMP_DIR/compose-missing" || {
    echo "FAIL: Compose missing-value error was not actionable" >&2
    exit 1
  }
  docker compose --env-file "$VALID" --profile ui \
    -f deploy/offline/docker-compose.offline.yml config --quiet
fi

bash scripts/ci/check-offline-secret-safety.sh >/dev/null
bash scripts/ci/test-offline-image-archive.sh >/dev/null
bash scripts/ci/test-scada-collect-safety.sh >/dev/null
echo "offline secret safety tests: OK"
