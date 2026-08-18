#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."

VERIFIER="$PWD/scripts/offline/verify-image-archive.py"
TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/edge-iot-image-test.XXXXXX")
trap 'rm -rf "$TMP_DIR"' EXIT

make_archive() {
  local destination=$1 mode=$2
  mkdir -p "$destination"
  python3 -I - "$destination" "$mode" <<'PY'
import hashlib
import io
import json
import pathlib
import sys
import tarfile

destination = pathlib.Path(sys.argv[1])
mode = sys.argv[2]
images = [
    ("edge-iot/backend:offline-amd64", "backend.json", "amd64"),
    ("edge-iot/web:offline-amd64", "web.json", "amd64"),
    ("redis:7.0.10", "redis.json", "amd64"),
]
if mode == "missing":
    images.pop()
elif mode == "extra":
    images.append(("unexpected/runtime:latest", "extra.json", "amd64"))
elif mode == "unexpected":
    images[-1] = ("unexpected/runtime:latest", images[-1][1], "amd64")
elif mode == "wrong-arch":
    images[0] = (images[0][0], images[0][1], "arm64")

manifest = []
payloads = {}
for tag, config_name, architecture in images:
    manifest.append({"Config": config_name, "RepoTags": [tag], "Layers": []})
    payloads[config_name] = json.dumps(
        {"architecture": architecture, "os": "linux"}, separators=(",", ":")
    ).encode()
payloads["manifest.json"] = json.dumps(manifest, separators=(",", ":")).encode()

archive_path = destination / "images.tar"
with tarfile.open(archive_path, "w") as archive:
    for name, payload in payloads.items():
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(payload))
digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
(destination / "images.tar.sha256").write_text(f"{digest}  images.tar\n", encoding="ascii")
PY
}

expect_fail() {
  local label=$1 directory=$2 expected=$3 output="$TMP_DIR/output"
  if python3 -I "$VERIFIER" "$directory/images.tar" "$directory/images.tar.sha256" \
      >"$output" 2>&1; then
    echo "FAIL: $label unexpectedly passed" >&2
    exit 1
  fi
  grep -Fq "$expected" "$output" || {
    echo "FAIL: $label did not report: $expected" >&2
    sed -n '1,20p' "$output" >&2
    exit 1
  }
}

VALID="$TMP_DIR/valid"
make_archive "$VALID" valid
python3 -I "$VERIFIER" "$VALID/images.tar" "$VALID/images.tar.sha256" >/dev/null

for mode in missing extra unexpected wrong-arch; do
  directory="$TMP_DIR/$mode"
  make_archive "$directory" "$mode"
  case "$mode" in
    missing|extra) reason='exactly three image entries' ;;
    unexpected) reason='unexpected image tag set' ;;
    wrong-arch) reason='is not linux/amd64' ;;
  esac
  expect_fail "$mode archive" "$directory" "$reason"
done

BAD_SUM="$TMP_DIR/bad-checksum"
cp -R "$VALID" "$BAD_SUM"
python3 -I - "$BAD_SUM/images.tar.sha256" <<'PY'
import pathlib, sys
pathlib.Path(sys.argv[1]).write_text("0" * 64 + "  images.tar\n", encoding="ascii")
PY
expect_fail "checksum mismatch" "$BAD_SUM" 'SHA-256 mismatch'

MISSING_SUM="$TMP_DIR/missing-checksum"
mkdir "$MISSING_SUM"
cp "$VALID/images.tar" "$MISSING_SUM/images.tar"
expect_fail "missing checksum" "$MISSING_SUM" 'No such file or directory'

echo "offline image archive tests: OK"
