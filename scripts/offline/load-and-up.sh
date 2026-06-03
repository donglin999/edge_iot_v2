#!/usr/bin/env bash
# ============================================================================
# Generic offline loader — run ON THE工控机 from inside an unpacked kit dir
# (it operates on ./images.tar + ./docker-compose.yml next to this script).
#
#   ./load-and-up.sh                 # docker load + verify amd64 + compose up
#   ./load-and-up.sh --env-file edge.env
#   ./load-and-up.sh --profile smoke # also start mock-modbus/mock-mqtt
#   ./load-and-up.sh --load-only     # just docker load + arch check, no up
#
# Extra args after the known flags are passed through to `docker compose up`.
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"

ENV_ARGS=(); UP_ARGS=(); LOAD_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --env-file) ENV_ARGS+=(--env-file "$2"); shift 2 ;;
    --profile)  UP_ARGS+=(--profile "$2"); shift 2 ;;
    --load-only) LOAD_ONLY=1; shift ;;
    *) UP_ARGS+=("$1"); shift ;;
  esac
done

echo "==> verifying images.tar checksum"
if [ -f images.tar.sha256 ]; then
  shasum -a 256 -c images.tar.sha256
else
  echo "   (no images.tar.sha256 — skipping)"
fi

echo "==> docker load < images.tar"
docker load -i images.tar

echo "==> arch self-check (expect amd64 on every edge-iot/* image)"
docker images --format '{{.Repository}}:{{.Tag}}' | grep -E 'edge-iot/|redis|influxdb|mosquitto|nginx|python' | sort -u | while read -r img; do
  arch=$(docker image inspect "$img" --format '{{.Architecture}}' 2>/dev/null || echo '?')
  printf '   %-44s %s\n' "$img" "$arch"
done

[ "$LOAD_ONLY" -eq 1 ] && { echo "load-only done."; exit 0; }

echo "==> docker compose up -d"
docker compose "${ENV_ARGS[@]}" -f docker-compose.yml up -d "${UP_ARGS[@]}"
docker compose -f docker-compose.yml ps
