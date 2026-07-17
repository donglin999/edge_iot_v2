#!/usr/bin/env bash
# ============================================================================
# 在构建宿主机上产出 amd64 wheelhouse 供离线后端镜像使用。在宿主机（任意架构，
# pip 按目标平台下 wheel）而非容器内运行，用的是宿主机的快网，而不是模拟容器
# 里 ~13 kB/s 的链路。
#
# 产物：deploy/offline/wheelhouse/*.whl（gitignore；由 Dockerfile.backend 用
# `pip install --no-index` 消费）。
#
# 镜像源可覆盖，默认清华（本机在 Asia/Shanghai）：
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

echo "   wheelhouse ready: $(ls "$WH" | wc -l | tr -d ' ') wheels, $(du -sh "$WH" | cut -f1)"
