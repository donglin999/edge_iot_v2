#!/usr/bin/env bash
#
# XIU-108 Phase 2 P5.2 — Playwright e2e bring-up + run script.
#
# Purpose: bring the docker-compose stack (center + 2 edges) to a known
# good state, run the four MQTT-form Playwright specs, and copy the
# evidence (screenshots, trace, json result) under /tmp/mqtt-evidence/.
#
# Re-runnable. Idempotent against an already-running stack.
#
# Required tooling on the host: docker, docker compose v2, node ≥ 18.
# Pre-req: edges must already exist in the center DB and have their
# one-shot activation token consumed, or new tokens must be exported
# via EDGE_A_TOKEN / EDGE_B_TOKEN before invoking this script.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

EDGE_A="${E2E_EDGE_A:-qa-xiu108-a}"
EDGE_B="${E2E_EDGE_B:-qa-xiu108-b}"
EVIDENCE_DIR="${E2E_EVIDENCE_DIR:-/tmp/mqtt-evidence}"
BASE_URL="${E2E_BASE_URL:-http://localhost:5173}"
API_URL="${E2E_API_URL:-http://localhost:8000}"

mkdir -p "$EVIDENCE_DIR"

note() { printf '\033[1;36m▸ %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

note "Bringing up center stack (docker-compose.center.yml)"
docker compose -f docker-compose.center.yml up -d --build

note "Waiting for /api/fleet/edges/ to be reachable on $API_URL"
for _ in $(seq 1 60); do
  if curl -fsS -o /dev/null "$API_URL/api/fleet/edges/?limit=1"; then break; fi
  sleep 2
done
curl -fsS -o /dev/null "$API_URL/api/fleet/edges/?limit=1" \
  || fail "center API never came up — check 'docker logs center-django'"

note "Ensuring 2 edge stacks ($EDGE_A, $EDGE_B) are up"
# Operators must export EDGE_A_TOKEN / EDGE_B_TOKEN if these edges are
# being brought up cold. The compose file requires EDGE_ID + EDGE_TOKEN.
for spec in "A:${EDGE_A}" "B:${EDGE_B}"; do
  letter="${spec%%:*}"
  edge_id="${spec##*:}"
  project="xiu108-${edge_id}"
  if docker compose -p "$project" -f docker-compose.edge.yml ps -q edge-agent | grep -q .; then
    note "  $edge_id already running"
    continue
  fi
  token_var="EDGE_${letter}_TOKEN"
  token="${!token_var:-}"
  if [[ -z "$token" ]]; then
    note "  $edge_id not running and $token_var not exported — skipping bring-up"
    note "  (re-export from a fresh POST /api/fleet/edges/ + retry)"
    continue
  fi
  history_port_var="EDGE_${letter}_HISTORY_PORT"
  history_port="${!history_port_var:-$(( 18086 + (letter == 'A' ? 0 : 1) ))}"
  EDGE_ID="$edge_id" \
    EDGE_TOKEN="$token" \
    CENTER_URL="ws://host.docker.internal:8000/ws/fleet/" \
    EDGE_TRANSPORT=mqtt \
    EDGE_MQTT_BROKER="mqtt://host.docker.internal:1883" \
    EDGE_HISTORY_HOST_PORT="$history_port" \
    docker compose -p "$project" -f docker-compose.edge.yml up -d --build
done

note "Waiting for both edges to come online"
for _ in $(seq 1 60); do
  body="$(curl -fsS "$API_URL/api/fleet/edges/?limit=200" || true)"
  online_count=$(python3 - <<PY
import json, sys
try:
    data = json.loads(${body@Q})
except Exception:
    print(0); sys.exit()
rows = data if isinstance(data, list) else data.get("results") or []
want = {"${EDGE_A}", "${EDGE_B}"}
print(sum(1 for r in rows if r.get("name") in want and r.get("status") == "online"))
PY
)
  if [[ "$online_count" == "2" ]]; then
    note "  both edges online"
    break
  fi
  sleep 2
done

note "Running Playwright"
pushd frontend >/dev/null
E2E_EDGE_A="$EDGE_A" \
E2E_EDGE_B="$EDGE_B" \
E2E_BASE_URL="$BASE_URL" \
E2E_API_URL="$API_URL" \
E2E_EVIDENCE_DIR="$EVIDENCE_DIR" \
  npx playwright test --project=chromium --reporter=list,html,json
popd >/dev/null

note "Copying evidence to $EVIDENCE_DIR"
EV_PLAYWRIGHT="$EVIDENCE_DIR/playwright-xiu108"
rm -rf "$EV_PLAYWRIGHT"
mkdir -p "$EV_PLAYWRIGHT"
cp -R frontend/e2e/.artifacts/screenshots "$EV_PLAYWRIGHT/" 2>/dev/null || true
cp -R frontend/e2e/.artifacts/html-report "$EV_PLAYWRIGHT/" 2>/dev/null || true
cp frontend/e2e/.artifacts/results.json "$EV_PLAYWRIGHT/" 2>/dev/null || true
# mosquitto admin screenshot is a manual capture; this script just
# reminds the operator.
if [[ ! -f "$EVIDENCE_DIR/mosquitto-admin.png" ]]; then
  note "  reminder: capture a screenshot of the mosquitto admin / sys-tree as $EVIDENCE_DIR/mosquitto-admin.png"
fi

note "Done. Evidence in $EVIDENCE_DIR"
