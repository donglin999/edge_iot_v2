#!/usr/bin/env bash
# ============================================================================
# 构建【中山小家电 离线单体 amd64 安装包】到 dist/offline/zhongshan-monolith-amd64/。
# 在联网的构建宿主机上运行（Mac/arm64 通过 buildx 跨架构 OK）。产物目录拷到
# U 盘带到无网工控机，跑 ./load-and-up.sh 一键起。
#
# 产出：
#   dist/offline/zhongshan-monolith-amd64/images.tar          （应用镜像，docker save）
#   dist/offline/zhongshan-monolith-amd64/images.tar.sha256
#   dist/offline/zhongshan-monolith-amd64/manifest.txt        （镜像清单 + 架构 + 大小）
#   dist/offline/zhongshan-monolith-amd64/{docker-compose.yml,load-and-up.sh,.env.example,README...}
#
# 注意：redis:7.0.10 / influxdb:2.4 **不打进包**（目标机已有）。仅 build+save
# 两个应用镜像 edge-iot/backend、edge-iot/web。
#
# 前置：docker buildx；且已在宿主机跑过 `cd frontend && npm ci && npm run build`
# （脚本会校验 frontend/dist 存在 —— 教训 frontend-build-on-host：SPA 不在跨架构
# 容器里构建）。
# ============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
OUT="dist/offline/zhongshan-monolith-amd64"
PLATFORM="linux/amd64"

# 目标机已有、不打包，仅在 README/清单里记录为前置要求。
# influxdb 由生产环境自行运行在宿主机（本包不启动它），故不列入。
PREEXISTING_IMAGES=(redis:7.0.10)
BUILT_IMAGES=(edge-iot/backend:offline-amd64 edge-iot/web:offline-amd64)

# 从 docker-save tar 内某镜像的 config blob 读架构（architecture 字段），让清单
# 反映实际发出的字节，而不是本机 by-name 镜像（Mac 上 by-name inspect 命中 arm64
# 缓存会误报）。保持 bash-3.2 兼容（Mac 自带无关联数组）。
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

echo "==> [1/5] 预检"
command -v docker >/dev/null || { echo "docker not found"; exit 1; }
docker buildx version >/dev/null || { echo "docker buildx required"; exit 1; }
[ -d frontend/dist ] || { echo "frontend/dist 缺失 —— 先跑: (cd frontend && npm ci && npm run build)"; exit 1; }
rm -rf "$OUT" && mkdir -p "$OUT"

echo "==> [2/5] 宿主机构建 amd64 wheelhouse"
bash scripts/offline/build-wheelhouse.sh

echo "==> [3/5] buildx 构建 amd64 应用镜像（依赖来自 wheelhouse，容器内零下载）"
docker buildx build --provenance=false --sbom=false --platform "$PLATFORM" \
  -f deploy/offline/Dockerfile.backend -t edge-iot/backend:offline-amd64 --load .
docker buildx build --provenance=false --sbom=false --platform "$PLATFORM" \
  -f deploy/offline/Dockerfile.web -t edge-iot/web:offline-amd64 --load .

echo "==> [4/5] docker save -> images.tar (+ sha256, manifest)"
docker save --platform "$PLATFORM" "${BUILT_IMAGES[@]}" -o "$OUT/images.tar"
( cd "$OUT" && shasum -a 256 images.tar > images.tar.sha256 )
: > "$OUT/manifest.txt"
echo "# 应用镜像（已打包在 images.tar 内）" >> "$OUT/manifest.txt"
for img in "${BUILT_IMAGES[@]}"; do
  arch=$(tar_arch "$OUT/images.tar" "$img")
  size=$(docker image inspect "$img" --format '{{.Size}}' 2>/dev/null || echo '?')
  printf '%-40s arch=%-6s size=%s\n' "$img" "$arch" "$size" >> "$OUT/manifest.txt"
done
echo "" >> "$OUT/manifest.txt"
echo "# 前置镜像（目标机必须已存在，未打包）" >> "$OUT/manifest.txt"
for img in "${PREEXISTING_IMAGES[@]}"; do echo "$img" >> "$OUT/manifest.txt"; done
echo "" >> "$OUT/manifest.txt"
echo "images.tar 大小: $(du -h "$OUT/images.tar" | cut -f1)" >> "$OUT/manifest.txt"

echo "==> [5/5] 打包运行材料"
cp deploy/offline/docker-compose.offline.yml "$OUT/docker-compose.yml"
cp scripts/offline/load-and-up.sh            "$OUT/"
cp scripts/offline/import-and-start.sh       "$OUT/"
cp scripts/offline/status.sh                 "$OUT/"
cp deploy/offline/.env.example               "$OUT/.env.example"
cp docs/deploy/offline-amd64-monolith.md     "$OUT/README.md" 2>/dev/null || true
chmod +x "$OUT/load-and-up.sh" "$OUT/import-and-start.sh" "$OUT/status.sh"

echo ""
echo "==> 完成。安装包目录："
echo "    $OUT"
echo "    ├── images.tar ($(du -h "$OUT/images.tar" | cut -f1)) + .sha256"
echo "    ├── docker-compose.yml / .env.example / load-and-up.sh / README.md"
echo "    └── manifest.txt"
cat "$OUT/manifest.txt"
