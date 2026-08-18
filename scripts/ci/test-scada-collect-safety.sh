#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."

SCRIPT="$PWD/scripts/scada_collect.py"
TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/edge-iot-scada-safety.XXXXXX")
trap 'rm -rf "$TMP_DIR"' EXIT

command -v openssl >/dev/null 2>&1 || {
  echo "FAIL: openssl is required for the SCADA TLS startup test" >&2
  exit 1
}
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -subj '/CN=edge-iot-test-ca.invalid' \
  -keyout "$TMP_DIR/ca.key" -out "$TMP_DIR/ca.pem" >/dev/null 2>&1
chmod 600 "$TMP_DIR/ca.key" "$TMP_DIR/ca.pem"

run_check() {
  env -i PATH="$PATH" \
    SCADA_MQTT_BROKER=broker.test.invalid \
    SCADA_MQTT_PORT="${SCADA_TEST_PORT:-8883}" \
    SCADA_MQTT_USERNAME=synthetic-user \
    SCADA_MQTT_PASSWORD=synthetic-password \
    SCADA_MQTT_CA_FILE="${SCADA_TEST_CA:-$TMP_DIR/ca.pem}" \
    SCADA_PRODUCT_KEY=synthetic-product \
    SCADA_DEVICE_NAME=synthetic-device \
    INFLUXDB_URL=http://127.0.0.1:8086 \
    INFLUXDB_TOKEN=synthetic-token \
    INFLUXDB_ORG=synthetic-org \
    INFLUXDB_BUCKET=synthetic-bucket \
    python3 -I "$SCRIPT" --check-config
}

if env -i PATH="$PATH" python3 -I "$SCRIPT" --check-config \
    >"$TMP_DIR/missing" 2>&1; then
  echo "FAIL: SCADA collector started without required configuration" >&2
  exit 1
fi
grep -Fq 'SCADA_MQTT_BROKER' "$TMP_DIR/missing"

run_check >"$TMP_DIR/valid" 2>&1
grep -Fq 'configuration and TLS CA verified' "$TMP_DIR/valid"
if grep -Fq 'synthetic-password' "$TMP_DIR/valid" \
    || grep -Fq 'synthetic-token' "$TMP_DIR/valid"; then
  echo "FAIL: SCADA startup check leaked a credential" >&2
  exit 1
fi

if SCADA_TEST_PORT=not-a-port run_check >"$TMP_DIR/port" 2>&1; then
  echo "FAIL: SCADA collector accepted an invalid port" >&2
  exit 1
fi
grep -Fq 'SCADA_MQTT_PORT must be an integer' "$TMP_DIR/port"

echo 'not a certificate' > "$TMP_DIR/bad-ca.pem"
chmod 600 "$TMP_DIR/bad-ca.pem"
if SCADA_TEST_CA="$TMP_DIR/bad-ca.pem" run_check >"$TMP_DIR/tls" 2>&1; then
  echo "FAIL: SCADA collector accepted an invalid TLS CA" >&2
  exit 1
fi
grep -Eq 'SSL|PEM|certificate' "$TMP_DIR/tls"

if grep -nE '(^|[^A-Z_])DEVICE_NAME([^A-Z_]|$)' "$SCRIPT"; then
  echo "FAIL: SCADA example/runtime still uses ambiguous DEVICE_NAME" >&2
  exit 1
fi

echo "SCADA collector configuration/TLS startup tests: OK"
