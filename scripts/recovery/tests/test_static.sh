#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
RECOVERY_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
REPO_ROOT="$(cd -- "$RECOVERY_DIR/../.." && pwd -P)"
PYCACHE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/m0-recovery-pycache.XXXXXX")"
trap 'rm -rf -- "$PYCACHE_ROOT"' EXIT
export PYTHONPYCACHEPREFIX="$PYCACHE_ROOT"

bash -n "$RECOVERY_DIR/m0_ci_drill.sh" "$SCRIPT_DIR/test_static.sh"
python3 -m py_compile \
  "$RECOVERY_DIR/safety.py" \
  "$RECOVERY_DIR/celery_smoke.py" \
  "$RECOVERY_DIR/dind_tls.py" \
  "$RECOVERY_DIR/evidence_io.py" \
  "$RECOVERY_DIR/image_archive.py" \
  "$RECOVERY_DIR/stack_smoke.py" \
  "$RECOVERY_DIR/seed_fixture.py"
python3 -m unittest discover -s "$SCRIPT_DIR" -p 'test_*.py' -v
python3 -m unittest discover -s "$RECOVERY_DIR/../backup/tests" -p 'test_*.py' -v

python3 "$RECOVERY_DIR/safety.py" compose "$RECOVERY_DIR/docker-compose.m0-ci.yml"
python3 "$RECOVERY_DIR/safety.py" recovery-scripts --root "$RECOVERY_DIR"

grep -q 'pull_policy: never' "$RECOVERY_DIR/docker-compose.m0-ci.yml"
grep -q 'host_ip: "127.0.0.1"' "$RECOVERY_DIR/docker-compose.m0-ci.yml"
grep -q 'internal: true' "$RECOVERY_DIR/docker-compose.m0-ci.yml"
grep -q 'mem_limit:' "$RECOVERY_DIR/docker-compose.m0-ci.yml"
grep -q 'pids_limit:' "$RECOVERY_DIR/docker-compose.m0-ci.yml"
grep -q 'M0_DJANGO_SECRET_KEY:?' "$RECOVERY_DIR/docker-compose.m0-ci.yml"
grep -q 'M0_INFLUX_INIT_PASSWORD:?' "$RECOVERY_DIR/docker-compose.m0-ci.yml"
grep -q 'COMPOSE_PROJECT_NAME="$PROJECT"' "$RECOVERY_DIR/m0_ci_drill.sh"
grep -q 'label=com.docker.compose.project=$PROJECT' "$RECOVERY_DIR/m0_ci_drill.sh"
grep -q 'M0_TOKEN_FILE="$ALLOWED_ROOT/' "$RECOVERY_DIR/m0_ci_drill.sh"
grep -q 'create-and-exec' "$RECOVERY_DIR/m0_ci_drill.sh"
grep -q 'run_evidence' "$RECOVERY_DIR/m0_ci_drill.sh"
grep -q 'O_EXCL' "$RECOVERY_DIR/evidence_io.py"
grep -q 'O_NOFOLLOW' "$RECOVERY_DIR/evidence_io.py"
grep -q 'docker network create --driver bridge --internal' "$RECOVERY_DIR/m0_ci_drill.sh"
grep -q -- '--memory 768m --cpus 1.50 --pids-limit 512' "$RECOVERY_DIR/m0_ci_drill.sh"
grep -q 'DOCKER_TLS_CERTDIR=/certs' "$RECOVERY_DIR/m0_ci_drill.sh"
grep -q '2376' "$RECOVERY_DIR/m0_ci_drill.sh"
grep -q 'openssl verify -CAfile /certs/client/ca.pem' "$RECOVERY_DIR/m0_ci_drill.sh"
grep -q '"$DIND_TLS" capture' "$RECOVERY_DIR/m0_ci_drill.sh"
grep -q 'docker exec "$DIND_ID" cat "/certs/client/$certificate"' "$RECOVERY_DIR/m0_ci_drill.sh"
grep -q -- '--tlsverify' "$RECOVERY_DIR/m0_ci_drill.sh"
grep -q -- '--docker-tls-key' "$RECOVERY_DIR/m0_ci_drill.sh"
grep -q '_stream_load_to_docker' "$RECOVERY_DIR/image_archive.py"
grep -q 'bytes delivered to Docker do not match archive checksum' "$RECOVERY_DIR/image_archive.py"
grep -q 'active_queues' "$RECOVERY_DIR/m0_ci_drill.sh"
grep -q -- '--json --timeout 10 --destination "$node"' "$RECOVERY_DIR/m0_ci_drill.sh"
grep -q 'set(payload) != {node}' "$RECOVERY_DIR/celery_smoke.py"
grep -q 'queues\[0\].get("name") != expected_queue' "$RECOVERY_DIR/celery_smoke.py"
grep -q 'runner-local evidence was not uploaded' "$RECOVERY_DIR/m0_ci_drill.sh"
if grep -q 'docker cp' "$RECOVERY_DIR/m0_ci_drill.sh"; then
  echo "mutable-path docker cp must not be used for DinD TLS material" >&2
  exit 1
fi
if grep -q 'actions/upload-artifact' "$REPO_ROOT/.github/workflows/m0-recovery-drill.yml"; then
  echo "runner-local recovery evidence must not have an artifact uploader" >&2
  exit 1
fi
if grep -q 'M0_EVIDENCE_PATH' "$REPO_ROOT/.github/workflows/m0-recovery-drill.yml"; then
  echo "workflow must not publish an evidence export path" >&2
  exit 1
fi

echo "M0 recovery static and unit gates passed"
