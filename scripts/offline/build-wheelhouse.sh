#!/usr/bin/env bash
# ============================================================================
# 在构建宿主机上产出 amd64 wheelhouse 供离线后端镜像使用。宿主机架构不限，但
# 解析器必须是 CPython 3.10：pip 的 --python-version 只控制 wheel 兼容标签，不会
# 把 dependency marker 的求值解释器切换成 3.10。用 3.11/3.13 解析会漏掉目标环境
# 需要的 tomli / async-timeout。依赖仍在宿主机下载，避免模拟容器约 ~13 kB/s 的
# 慢链路。
#
# 产物：deploy/offline/wheelhouse/*.whl（gitignore；由 Dockerfile.backend 用
# `pip install --no-index` 消费）。
#
# 镜像源可覆盖，默认清华（本机在 Asia/Shanghai）：
#   PIP_MIRROR=https://pypi.org/simple bash scripts/offline/build-wheelhouse.sh
# Python 可显式覆盖，但必须是 CPython 3.10：
#   PYTHON_BIN=/opt/python3.10/bin/python3.10 bash scripts/offline/build-wheelhouse.sh
# 仅检查解析器、不创建或下载 wheelhouse：
#   bash scripts/offline/build-wheelhouse.sh --check-python
# ============================================================================
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

WH="deploy/offline/wheelhouse"
PIP_MIRROR="${PIP_MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}"
PLATFORMS=(--platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 --platform manylinux_2_28_x86_64)

case "${1:-}" in
  "") CHECK_PYTHON_ONLY=false ;;
  --check-python) CHECK_PYTHON_ONLY=true ;;
  *)
    echo "usage: $0 [--check-python]" >&2
    exit 2
    ;;
esac
[ "$#" -le 1 ] || { echo "usage: $0 [--check-python]" >&2; exit 2; }

resolve_python310() {
  local candidate resolved info
  local candidates=()

  if [ -n "${PYTHON_BIN:-}" ]; then
    candidates=("$PYTHON_BIN")
  else
    # Prefer the explicit executable, while also accepting python3 when it is
    # itself CPython 3.10. Never silently fall back to another minor version.
    candidates=(python3.10 python3)
  fi

  for candidate in "${candidates[@]}"; do
    resolved="$(command -v "$candidate" 2>/dev/null || true)"
    [ -n "$resolved" ] || continue
    info="$("$resolved" -c \
      'import sys; print(f"{sys.implementation.name} {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")' \
      2>/dev/null || true)"
    case "$info" in
      "cpython 3.10."*)
        PY="$resolved"
        PY_INFO="$info"
        break
        ;;
    esac

    # An explicit override is a contract: fail instead of searching elsewhere.
    if [ -n "${PYTHON_BIN:-}" ]; then
      echo "wheelhouse requires CPython 3.10; PYTHON_BIN resolved to '${info:-unknown}' ($resolved)" >&2
      exit 2
    fi
  done

  if [ -z "${PY:-}" ]; then
    echo "wheelhouse requires CPython 3.10; install python3.10 or set PYTHON_BIN to its executable" >&2
    exit 2
  fi
  if ! "$PY" -m pip --version >/dev/null 2>&1; then
    echo "wheelhouse requires pip for $PY ($PY_INFO)" >&2
    exit 2
  fi
}

resolve_python310
echo "   resolver: $PY ($PY_INFO)"

if [ "$CHECK_PYTHON_ONLY" = true ]; then
  exit 0
fi

rm -rf "$WH" && mkdir -p "$WH"

echo "   wheelhouse: backend/requirements.txt + constraints-py310.txt -> $WH (mirror=$PIP_MIRROR)"
"$PY" -m pip download -r backend/requirements.txt \
  --constraint backend/constraints-py310.txt \
  "${PLATFORMS[@]}" --only-binary=:all: --python-version 3.10 \
  --implementation cp --abi cp310 \
  -i "$PIP_MIRROR" -d "$WH" >/dev/null

echo "   wheelhouse ready: $(ls "$WH" | wc -l | tr -d ' ') wheels, $(du -sh "$WH" | cut -f1)"
