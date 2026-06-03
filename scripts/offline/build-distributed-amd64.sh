#!/usr/bin/env bash
# ============================================================================
# Build the OFFLINE DISTRIBUTED (distributed/main, incl. merged lwt fix
# XIU-112) amd64 install kit into dist/offline/distributed-amd64/.
#
# !! Run only AFTER XIU-126 merge regression passes — the distributed kit MUST
#    be built from the MERGED distributed/main tip, not this feature branch.
#    Verify: git -C <repo> log --oneline -1 distributed/main  (expect the merge).
#
# Produces two sub-kits under dist/offline/distributed-amd64/:
#   center/  -> images.tar (backend, web, redis, mosquitto) + compose + scripts
#   edge/    -> images.tar (edge-agent, influxdb, python) + compose + env + scripts
# each with images.tar.sha256 + manifest.txt.
#
# Prereqs: docker buildx; frontend/dist built on host (frontend-build-on-host).
# ============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
ROOT="dist/offline/distributed-amd64"
PLATFORM="linux/amd64"

echo "==> preflight"
command -v docker >/dev/null || { echo "docker not found"; exit 1; }
docker buildx version >/dev/null || { echo "docker buildx required"; exit 1; }
[ -d frontend/dist ] || { echo "frontend/dist missing — run: (cd frontend && npm ci && npm run build)"; exit 1; }
mkdir -p "$ROOT/center" "$ROOT/edge"

save_kit () { # $1=outdir ; rest=images
  local out="$1"; shift
  docker save "$@" -o "$out/images.tar"
  ( cd "$out" && shasum -a 256 images.tar > images.tar.sha256 )
  : > "$out/manifest.txt"
  for img in "$@"; do
    printf '%-44s arch=%s size=%s\n' "$img" \
      "$(docker image inspect "$img" --format '{{.Architecture}}')" \
      "$(docker image inspect "$img" --format '{{.Size}}')" >> "$out/manifest.txt"
  done
  echo "tar size: $(du -h "$out/images.tar" | cut -f1)" >> "$out/manifest.txt"
}

echo "==> pull amd64 base images"
for img in redis:7-alpine eclipse-mosquitto:2 influxdb:2.7 python:3.10-slim; do
  docker pull --platform "$PLATFORM" "$img"
done

echo "==> buildx amd64 application images"
docker buildx build --platform "$PLATFORM" -f backend/Dockerfile \
  -t edge-iot/backend:offline-amd64 --load backend
docker buildx build --platform "$PLATFORM" -f deploy/offline/Dockerfile.web \
  -t edge-iot/web:offline-amd64 --load .
docker buildx build --platform "$PLATFORM" -f deploy/offline/Dockerfile.edge-agent \
  -t edge-iot/edge-agent:offline-amd64 --load .

echo "==> CENTER kit"
save_kit "$ROOT/center" edge-iot/backend:offline-amd64 edge-iot/web:offline-amd64 redis:7-alpine eclipse-mosquitto:2
cp deploy/offline/docker-compose.center.offline.yml "$ROOT/center/docker-compose.yml"
cp scripts/offline/load-and-up.sh "$ROOT/center/"
mkdir -p "$ROOT/center/mosquitto" && cp mosquitto/mosquitto.conf "$ROOT/center/mosquitto/"

echo "==> EDGE kit"
save_kit "$ROOT/edge" edge-iot/edge-agent:offline-amd64 influxdb:2.7 python:3.10-slim
cp deploy/offline/docker-compose.edge.offline.yml "$ROOT/edge/docker-compose.yml"
cp deploy/offline/edge.env.example "$ROOT/edge/"
cp scripts/offline/load-and-up.sh "$ROOT/edge/"
mkdir -p "$ROOT/edge/mock" && cp mock/entrypoint.sh mock/modbus_realistic.py "$ROOT/edge/mock/" 2>/dev/null || true

echo "DONE -> $ROOT"
echo "--- center ---"; cat "$ROOT/center/manifest.txt"
echo "--- edge ---";   cat "$ROOT/edge/manifest.txt"
