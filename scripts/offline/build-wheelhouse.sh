#!/usr/bin/env bash
# ============================================================================
# Build a HOST amd64 wheelhouse for the offline images. Runs on the build host
# (any arch — pip fetches target-platform wheels), NOT in a container, so it
# uses the host's fast network instead of the ~13 kB/s emulated-container link.
#
# Output: deploy/offline/wheelhouse/*.whl  (gitignored; consumed by
# Dockerfile.backend / Dockerfile.edge-agent via `pip install --no-index`).
#
# Mirror is overridable; defaults to Tsinghua (this fleet runs Asia/Shanghai).
#   PIP_MIRROR=https://pypi.org/simple bash scripts/offline/build-wheelhouse.sh
# ============================================================================
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

WH="deploy/offline/wheelhouse"
PIP_MIRROR="${PIP_MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}"
PY="${PYTHON_BIN:-python3}"
PLATFORMS=(--platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 --platform manylinux_2_28_x86_64)

rm -rf "$WH" && mkdir -p "$WH"

echo "   wheelhouse: backend/requirements.txt -> $WH (mirror=$PIP_MIRROR)"
"$PY" -m pip download -r backend/requirements.txt \
  "${PLATFORMS[@]}" --only-binary=:all: --python-version 3.10 \
  -i "$PIP_MIRROR" -d "$WH" >/dev/null

# edge-agent's own runtime deps (only present when the edge Dockerfile is built;
# harmless to always include — a few hundred KB).
if [ -f edge-agent/pyproject.toml ]; then
  echo "   wheelhouse: edge-agent deps (websockets/aiohttp/aiomqtt)"
  "$PY" -m pip download "websockets>=12.0" "aiohttp>=3.9" "aiomqtt>=2.3,<3" \
    "${PLATFORMS[@]}" --only-binary=:all: --python-version 3.10 \
    -i "$PIP_MIRROR" -d "$WH" >/dev/null
fi

echo "   wheelhouse ready: $(ls "$WH" | wc -l | tr -d ' ') wheels, $(du -sh "$WH" | cut -f1)"
