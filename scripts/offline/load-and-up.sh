#!/usr/bin/env bash
# ============================================================================
# 离线一键加载器 —— 在工控机上、解包目录内运行（操作同目录的 ./images.tar +
# ./docker-compose.yml）。
#
#   ./load-and-up.sh                 # 完整模式(带界面)：配置用
#   ./load-and-up.sh --headless      # 采集模式(无界面)：省资源，日常跑
#   ./load-and-up.sh --env-file .env # 用 .env 覆盖默认配置
#   ./load-and-up.sh --load-only     # 只 docker load + 检查，不 up
#
# 两种模式：
#   完整模式 = redis + migrate + celery + django + web（能开网页配置）
#   采集模式 = redis + migrate + celery（不起 django/web，省 ~100-150MB）
#             采集照跑，启动时自动恢复上次 RUNNING 的会话。
#   切换：停界面 docker compose stop django web ；开界面 --profile ui up -d
#
# 已知 flag 之后的额外参数原样透传给 `docker compose up`。
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"

ENV_ARGS=(); UP_ARGS=(); LOAD_ONLY=0; PROFILE_ARGS=(--profile ui); MODE="完整模式(带界面)"
while [ $# -gt 0 ]; do
  case "$1" in
    --env-file) ENV_ARGS+=(--env-file "$2"); shift 2 ;;
    --load-only) LOAD_ONLY=1; shift ;;
    --headless) PROFILE_ARGS=(); MODE="采集模式(无界面)"; shift ;;
    --ui) PROFILE_ARGS=(--profile ui); MODE="完整模式(带界面)"; shift ;;
    *) UP_ARGS+=("$1"); shift ;;
  esac
done

# redis:7.0.10 自 2026-07-27 起打进 images.tar(现场实测目标机没有该镜像且无网,
# "目标机已有"的假设不成立)。docker load 后统一自检,这里不再前置拦截。
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

echo "==> 镜像自检（三个镜像应齐全且均为 amd64）"
MISSING=0
for img in edge-iot/backend:offline-amd64 edge-iot/web:offline-amd64 redis:7.0.10; do
  if docker image inspect "$img" >/dev/null 2>&1; then
    arch=$(docker image inspect "$img" --format '{{.Architecture}}' 2>/dev/null || echo '?')
    printf '   %-44s %s\n' "$img" "$arch"
  else
    printf '   %-44s [缺失]\n' "$img"; MISSING=1
  fi
done
[ "$MISSING" -eq 1 ] && { echo "!! 有镜像缺失 —— images.tar 不完整？"; exit 1; }

[ "$LOAD_ONLY" -eq 1 ] && { echo "load-only 完成。"; exit 0; }

# 自动识别 compose：优先 v2 插件（docker compose），回退 v1（docker-compose）
if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  DC=(docker-compose)
else
  echo "!! 未找到 docker compose(v2) 或 docker-compose(v1)，请先安装其一。"; exit 1
fi
echo "==> 启动模式：${MODE}"
echo "==> ${DC[*]} ${PROFILE_ARGS[*]} up -d"
"${DC[@]}" "${ENV_ARGS[@]}" "${PROFILE_ARGS[@]}" -f docker-compose.yml up -d "${UP_ARGS[@]}"
echo ""
"${DC[@]}" "${PROFILE_ARGS[@]}" -f docker-compose.yml ps
echo ""
if [ ${#PROFILE_ARGS[@]} -gt 0 ]; then
  echo "==> 健康检查：稍候片刻后访问  http://<本机IP>:${WEB_PORT:-80}/   应返回前端页面"
  echo "    配置完成后可切到省资源的采集模式： ${DC[*]} stop django web"
else
  echo "==> 采集模式已启动（无界面）。采集在 celery-acq 中运行，会自动恢复上次 RUNNING 的会话。"
  echo "    需要配置时开界面： ${DC[*]} --profile ui -f docker-compose.yml up -d"
fi
echo "    查看采集日志： docker logs celery-acq --tail 50"
