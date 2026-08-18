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

# Invalid configuration must stop the loader before its first Docker call.
FAKE_BIN="$TMP_DIR/bin"
DOCKER_MARKER="$TMP_DIR/docker-was-called"
mkdir "$FAKE_BIN"
{
  echo '#!/usr/bin/env bash'
  printf 'touch %q\n' "$DOCKER_MARKER"
  echo 'exit 99'
} > "$FAKE_BIN/docker"
chmod +x "$FAKE_BIN/docker"
LOADER_OUTPUT="$TMP_DIR/loader-output"
if PATH="$FAKE_BIN:$PATH" scripts/offline/load-and-up.sh --env-file "$INCOMPLETE" \
    >"$LOADER_OUTPUT" 2>&1; then
  echo "FAIL: loader accepted incomplete configuration" >&2
  exit 1
fi
[ ! -e "$DOCKER_MARKER" ] || {
  echo "FAIL: loader invoked Docker before configuration validation" >&2
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
echo "offline secret safety tests: OK"
