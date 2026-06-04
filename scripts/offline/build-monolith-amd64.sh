#!/usr/bin/env bash
# ============================================================================
# Build the OFFLINE MONOLITH (master) amd64 install kit into dist/offline/
# monolith-amd64/. Run on a NETWORKED build host (Mac/arm64 ok via buildx).
# The output dir is copied to a U-disk and carried to the offline工控机.
#
# Produces:
#   dist/offline/monolith-amd64/images.tar         (all images, docker save)
#   dist/offline/monolith-amd64/images.tar.sha256
#   dist/offline/monolith-amd64/manifest.txt       (image list + sizes)
#   dist/offline/monolith-amd64/{compose,scripts,mock,...}  (run material)
#
# Prereqs: docker buildx, and `cd frontend && npm ci && npm run build` already
# run on the host (this script verifies frontend/dist exists — see lesson
# frontend-build-on-host: do NOT build the SPA inside a cross-arch container).
# ============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
OUT="dist/offline/monolith-amd64"
PLATFORM="linux/amd64"

BASE_IMAGES=(redis:7-alpine influxdb:2.7 eclipse-mosquitto:2 python:3.10-slim)
BUILT_IMAGES=(edge-iot/backend:offline-amd64 edge-iot/web:offline-amd64)

# Print the architecture of one image inside a docker-save tar, read from that
# image's config blob (architecture field) so the manifest reflects the bytes
# actually shipped — not the local by-name image (XIU-140). Kept bash-3.2 safe
# (no associative arrays — /usr/bin/env bash is 3.2 on the Mac build host).
tar_arch () { # $1=tar $2=repotag
  python3 - "$1" "$2" <<'PY'
import json, sys, tarfile
tar, want = sys.argv[1], sys.argv[2]
with tarfile.open(tar) as t:
    for e in json.load(t.extractfile('manifest.json')):
        if want in (e.get('RepoTags') or []):
            print(json.load(t.extractfile(e['Config'])).get('architecture', 'unknown'))
            break
    else:
        print('unknown')
PY
}

echo "==> [1/5] preflight"
command -v docker >/dev/null || { echo "docker not found"; exit 1; }
docker buildx version >/dev/null || { echo "docker buildx required"; exit 1; }
[ -d frontend/dist ] || { echo "frontend/dist missing — run: (cd frontend && npm ci && npm run build)"; exit 1; }
mkdir -p "$OUT"

echo "==> [2/5] build host amd64 wheelhouse + pull amd64 base images"
bash scripts/offline/build-wheelhouse.sh
for img in "${BASE_IMAGES[@]}"; do
  docker pull --platform "$PLATFORM" "$img"
done

echo "==> [3/5] buildx amd64 application images (deps from wheelhouse, no in-container DL)"
docker buildx build --provenance=false --sbom=false --platform "$PLATFORM" -f deploy/offline/Dockerfile.backend \
  -t edge-iot/backend:offline-amd64 --load .
docker buildx build --provenance=false --sbom=false --platform "$PLATFORM" -f deploy/offline/Dockerfile.web \
  -t edge-iot/web:offline-amd64 --load .

echo "==> [4/5] docker save -> images.tar (+ sha256, manifest)"
ALL_IMAGES=("${BASE_IMAGES[@]}" "${BUILT_IMAGES[@]}")
docker save --platform "$PLATFORM" "${ALL_IMAGES[@]}" -o "$OUT/images.tar"
( cd "$OUT" && shasum -a 256 images.tar > images.tar.sha256 )
# arch is read from the SAVED tar's image config blob (the actual amd64 payload),
# NOT `docker image inspect <name>` — by-name inspect hits the local arm64 cache on
# a Mac build host and misreports the kit arch (XIU-140). Size still from inspect.
: > "$OUT/manifest.txt"
for img in "${ALL_IMAGES[@]}"; do
  arch=$(tar_arch "$OUT/images.tar" "$img")
  size=$(docker image inspect "$img" --format '{{.Size}}')
  printf '%-40s arch=%-6s size=%s\n' "$img" "$arch" "$size" >> "$OUT/manifest.txt"
done
echo "tar size: $(du -h "$OUT/images.tar" | cut -f1)" >> "$OUT/manifest.txt"

echo "==> [5/5] stage run material"
cp deploy/offline/docker-compose.monolith.offline.yml "$OUT/docker-compose.yml"
cp scripts/offline/load-and-up.sh "$OUT/"
mkdir -p "$OUT/mock" && cp mock/entrypoint.sh mock/modbus_realistic.py mock/mosquitto.conf "$OUT/mock/" 2>/dev/null || true

echo "DONE -> $OUT"
cat "$OUT/manifest.txt"
