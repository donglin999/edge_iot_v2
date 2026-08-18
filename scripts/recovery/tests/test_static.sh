#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
RECOVERY_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
PYCACHE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/m0-recovery-pycache.XXXXXX")"
trap 'rm -rf -- "$PYCACHE_ROOT"' EXIT
export PYTHONPYCACHEPREFIX="$PYCACHE_ROOT"

bash -n "$RECOVERY_DIR/m0_ci_drill.sh" "$SCRIPT_DIR/test_static.sh"
python3 -m py_compile \
  "$RECOVERY_DIR/safety.py" \
  "$RECOVERY_DIR/image_archive.py" \
  "$RECOVERY_DIR/stack_smoke.py" \
  "$RECOVERY_DIR/seed_fixture.py"
python3 -m unittest discover -s "$SCRIPT_DIR" -p 'test_*.py' -v

if rg -n 'container_name:|network_mode:|external:[[:space:]]*(true|\{)' \
  "$RECOVERY_DIR/docker-compose.m0-ci.yml"; then
  echo "recovery Compose must not name containers, use host networking, or attach external resources" >&2
  exit 1
fi

if rg -n 'docker[[:space:]]+(system|image|volume|network)[[:space:]]+prune|docker[[:space:]]+compose[[:space:]]+down' \
  "$RECOVERY_DIR" -g '!tests/test_static.sh'; then
  echo "recovery scripts contain a broad cleanup primitive" >&2
  exit 1
fi

grep -q 'pull_policy: never' "$RECOVERY_DIR/docker-compose.m0-ci.yml"
grep -q 'host_ip: "127.0.0.1"' "$RECOVERY_DIR/docker-compose.m0-ci.yml"
grep -q 'internal: true' "$RECOVERY_DIR/docker-compose.m0-ci.yml"
grep -q 'COMPOSE_PROJECT_NAME="$PROJECT"' "$RECOVERY_DIR/m0_ci_drill.sh"
grep -q 'label=com.docker.compose.project=$PROJECT' "$RECOVERY_DIR/m0_ci_drill.sh"

echo "M0 recovery static and unit gates passed"
