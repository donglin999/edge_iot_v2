#!/usr/bin/env bash
# Exercise build-wheelhouse.sh's interpreter contract without touching the
# wheelhouse or accessing the network.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BUILDER="$REPO_ROOT/scripts/offline/build-wheelhouse.sh"
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/edge-iot-wheel-python-test.XXXXXX")"
trap 'rm -rf "$TMP_ROOT"' EXIT

FAKE_PYTHON="$TMP_ROOT/fake-python"
FAKE_BIN="$TMP_ROOT/bin"
mkdir -p "$FAKE_BIN"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -eu' \
  'if [ "${1:-}" = "-c" ]; then' \
  '  echo "${FAKE_PYTHON_INFO:-cpython 3.10.99}"' \
  'elif [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip" ] && [ "${3:-}" = "--version" ]; then' \
  '  echo "pip 23.0.1 (fake)"' \
  'else' \
  '  exit 97' \
  'fi' >"$FAKE_PYTHON"
chmod +x "$FAKE_PYTHON"
ln -s "$FAKE_PYTHON" "$FAKE_BIN/python3.10"

expect_fail_with() {
  local expected="$1"
  shift
  if output="$("$@" 2>&1)"; then
    echo "expected command to fail: $*" >&2
    exit 1
  fi
  case "$output" in
    *"$expected"*) ;;
    *)
      echo "failure did not include '$expected'" >&2
      echo "$output" >&2
      exit 1
      ;;
  esac
}

FAKE_PYTHON_INFO='cpython 3.10.99' PYTHON_BIN="$FAKE_PYTHON" \
  bash "$BUILDER" --check-python >/dev/null

expect_fail_with 'requires CPython 3.10' \
  env FAKE_PYTHON_INFO='cpython 3.11.9' PYTHON_BIN="$FAKE_PYTHON" \
  bash "$BUILDER" --check-python

expect_fail_with 'requires CPython 3.10' \
  env PYTHON_BIN="$TMP_ROOT/does-not-exist" bash "$BUILDER" --check-python

(
  unset PYTHON_BIN
  FAKE_PYTHON_INFO='cpython 3.10.42' PATH="$FAKE_BIN:$PATH" \
    bash "$BUILDER" --check-python >/dev/null
)

echo "wheelhouse Python 3.10 resolver self-test passed."
