#!/usr/bin/env bash
# ============================================================================
# 离线一键加载器 —— 在工控机上、解包目录内运行（操作同目录的 ./images.tar +
# ./docker-compose.yml）。
#
#   ./load-and-up.sh                 # docker load + 架构自检 + compose up
#   ./load-and-up.sh --env-file .env # 用 .env 覆盖默认配置
#   ./load-and-up.sh --load-only     # 只 docker load + 检查，不 up
#
# 已知 flag 之后的额外参数原样透传给 `docker compose up`。
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"

ENV_ARGS=(); UP_ARGS=(); LOAD_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --env-file) ENV_ARGS+=(--env-file "$2"); shift 2 ;;
    --load-only) LOAD_ONLY=1; shift ;;
    *) UP_ARGS+=("$1"); shift ;;
  esac
done

echo "==> 前置检查：目标机须已有 redis:7.0.10 镜像（本包不含它）"
if docker image inspect redis:7.0.10 >/dev/null 2>&1; then
  echo "   [有] redis:7.0.10"
else
  echo "   [缺] redis:7.0.10  <== 目标机缺此镜像且无网络。若宿主机已自跑 Redis，"
  echo "        请删除 docker-compose.yml 里的 redis 服务；否则先 docker load 该镜像。"
  exit 1
fi
echo "==> 提醒：InfluxDB 由生产环境自行运行，需可通过 127.0.0.1:8086 访问（org=Midea/bucket=Record）"
if command -v curl >/dev/null 2>&1; then
  curl -fsS "http://127.0.0.1:8086/health" >/dev/null 2>&1 \
    && echo "   [OK] 127.0.0.1:8086 可达" \
    || echo "   [!] 127.0.0.1:8086 暂不可达 —— 确认宿主机 InfluxDB 已启动，否则数据写不进去"
fi

echo "==> 校验 images.tar 校验和"
if [ -f images.tar.sha256 ]; then
  shasum -a 256 -c images.tar.sha256 2>/dev/null || sha256sum -c images.tar.sha256
else
  echo "   (无 images.tar.sha256 —— 跳过)"
fi

echo "==> docker load < images.tar"
docker load -i images.tar

echo "==> 架构自检（edge-iot/* 应均为 amd64）"
docker images --format '{{.Repository}}:{{.Tag}}' | grep -E 'edge-iot/|redis:7.0.10' | sort -u | while read -r img; do
  arch=$(docker image inspect "$img" --format '{{.Architecture}}' 2>/dev/null || echo '?')
  printf '   %-44s %s\n' "$img" "$arch"
done

[ "$LOAD_ONLY" -eq 1 ] && { echo "load-only 完成。"; exit 0; }

# 自动识别 compose：优先 v2 插件（docker compose），回退 v1（docker-compose）
if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  DC=(docker-compose)
else
  echo "!! 未找到 docker compose(v2) 或 docker-compose(v1)，请先安装其一。"; exit 1
fi
echo "==> ${DC[*]} up -d"
"${DC[@]}" "${ENV_ARGS[@]}" -f docker-compose.yml up -d "${UP_ARGS[@]}"
echo ""
"${DC[@]}" -f docker-compose.yml ps
echo ""
echo "==> 健康检查：稍候片刻后访问  http://<本机IP>/   应返回前端页面"
echo "    命令行自检： curl -fsS http://localhost/ >/dev/null && echo OK"
