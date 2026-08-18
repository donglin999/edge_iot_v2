#!/usr/bin/env bash
# ============================================================================
# 离线一键加载器 —— 在工控机上、解包目录内运行（操作同目录的 ./images.tar +
# ./docker-compose.yml）。
#
#   ./load-and-up.sh --env-file .env             # 完整模式(带界面)
#   ./load-and-up.sh --env-file .env --headless  # 采集模式(无界面)
#   ./load-and-up.sh --load-only     # 只 docker load + 检查，不 up
#
# 两种模式：
#   完整模式 = redis + migrate + celery + django + web（能开网页配置）
#   采集模式 = redis + migrate + celery（不起 django/web，省 ~100-150MB）
#             采集照跑，启动时自动恢复上次 RUNNING 的会话。
#   切换：停界面 docker stop django-iot frontend-iot；开界面重跑本脚本 --ui。
#
# 已知 flag 之后的额外参数原样透传给 `docker compose up`。
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"

ENV_ARGS=(); ENV_FILE=""; ENV_FILE_DISPLAY=""; UP_ARGS=(); LOAD_ONLY=0; PROFILE_ARGS=(--profile ui); MODE="完整模式(带界面)"
RUNTIME_ENV_DIR=""

cleanup_runtime_env() {
  if [ -n "$RUNTIME_ENV_DIR" ] && [ -d "$RUNTIME_ENV_DIR" ]; then
    case "$RUNTIME_ENV_DIR" in
      /tmp/edge-iot-runtime-env.*)
        rm -f -- "$RUNTIME_ENV_DIR/runtime.env"
        rmdir -- "$RUNTIME_ENV_DIR" 2>/dev/null || true
        ;;
      *) echo "!! 拒绝清理异常临时路径: $RUNTIME_ENV_DIR" >&2 ;;
    esac
  fi
}
trap cleanup_runtime_env EXIT
trap 'exit 130' HUP INT TERM

env_value() {
  local key=$1 fallback=${2:-}
  local value
  value=$(awk -v wanted="$key" '
    index($0, wanted "=") == 1 { print substr($0, length(wanted) + 2) }
  ' "$ENV_FILE")
  value=${value%$'\r'}
  printf '%s' "${value:-$fallback}"
}

verify_runtime_env() {
  python3 -I ./prepare-env.py verify "$ENV_FILE" "$ENV_RECEIPT"
}

run_compose() {
  # Start Compose from an allow-listed environment.  This also protects against
  # future Compose variables that are not yet known to this loader.
  local clean_env=(env -i "PATH=$PATH") key
  for key in HOME LANG LC_ALL LC_CTYPE DOCKER_HOST DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH DOCKER_CONFIG XDG_CONFIG_HOME; do
    if [ "${!key+x}" = x ]; then
      clean_env+=("$key=${!key}")
    fi
  done
  "${clean_env[@]}" "${DC[@]}" "$@"
}

while [ $# -gt 0 ]; do
  case "$1" in
    # WEB_PORT 单独记下来:它只进 compose 的变量空间,结尾的健康检查提示要打真实
    # 端口就得自己从 env 文件里读,否则永远显示默认 :80 误导现场。
    --env-file) [ "$#" -ge 2 ] || { echo "!! --env-file 缺少文件路径" >&2; exit 2; }
                [ -z "$ENV_FILE_DISPLAY" ] || { echo "!! --env-file 只能指定一次" >&2; exit 2; }
                ENV_FILE_DISPLAY=$2
                shift 2 ;;
    --load-only) LOAD_ONLY=1; shift ;;
    --headless) PROFILE_ARGS=(); MODE="采集模式(无界面)"; shift ;;
    --ui) PROFILE_ARGS=(--profile ui); MODE="完整模式(带界面)"; shift ;;
    *) UP_ARGS+=("$1"); shift ;;
  esac
done

command -v python3 >/dev/null 2>&1 || {
  echo "!! 缺少 python3，无法安全校验 env 与镜像包" >&2; exit 2
}

# 必须在 docker load、删容器或停旧服务之前 fail closed。
# --load-only 不运行应用，因此不需要生产凭据。
if [ "$LOAD_ONLY" -ne 1 ]; then
  [ -n "$ENV_FILE_DISPLAY" ] || {
    echo "!! 启动必须显式指定受保护的 env 文件: --env-file .env" >&2
    exit 2
  }
  for helper in ./validate-env.sh ./prepare-env.py; do
    [ -x "$helper" ] || { echo "!! 缺少可执行的 $helper，离线包不完整" >&2; exit 2; }
  done

  # O_NOFOLLOW 打开原文件、核对 owner/mode/inode，再复制到仅当前用户可读的
  # 私有快照。Compose 只读快照，原路径校验后被替换也无法改变生效配置。
  umask 077
  RUNTIME_ENV_DIR=$(mktemp -d /tmp/edge-iot-runtime-env.XXXXXX)
  chmod 700 "$RUNTIME_ENV_DIR"
  ENV_FILE="$RUNTIME_ENV_DIR/runtime.env"
  ENV_RECEIPT=$(python3 -I ./prepare-env.py prepare "$ENV_FILE_DISPLAY" "$ENV_FILE")
  ./validate-env.sh "$ENV_FILE"
  verify_runtime_env

  VALIDATED_WEB_PORT=$(env_value WEB_PORT 80)
  VALIDATED_INFLUXDB_HOST=$(env_value INFLUXDB_HOST)
  VALIDATED_INFLUXDB_PORT=$(env_value INFLUXDB_PORT)
  VALIDATED_LEGACY_CONTAINERS=$(env_value LEGACY_CONTAINERS)
  ENV_ARGS=(--env-file "$ENV_FILE")

  # 调用者 shell env 的优先级高于 --env-file。显式清理全部 Compose
  # 插值键和 Compose 控制键，确保唯一生效来源是已验证的私有快照。
  unset DJANGO_DB_NAME SECRET_KEY DEBUG ALLOWED_HOSTS ALLOW_PRIVATE_NETWORK_TESTS
  unset REDIS_HOST REDIS_PORT INFLUXDB_HOST INFLUXDB_PORT INFLUXDB_TOKEN
  unset INFLUXDB_ORG INFLUXDB_BUCKET INFLUX_SPILL_DB_PATH
  unset CELERY_ACQ_CONCURRENCY CELERY_SHORT_CONCURRENCY BACKEND_UPSTREAM WEB_PORT
  unset LEGACY_CONTAINERS
  unset COMPOSE_FILE COMPOSE_PROFILES COMPOSE_PROJECT_NAME COMPOSE_ENV_FILES COMPOSE_PATH_SEPARATOR

  # 自动识别 compose，并先做静默渲染校验。config 输出中含密钥，
  # 严禁直接打印或 tee；校验失败时也不会修改任何运行中容器。
  if docker compose version >/dev/null 2>&1; then
    DC=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    DC=(docker-compose)
  else
    echo "!! 未找到 docker compose(v2) 或 docker-compose(v1)，请先安装其一。"; exit 1
  fi
  verify_runtime_env
  run_compose "${ENV_ARGS[@]}" "${PROFILE_ARGS[@]}" -f docker-compose.yml config --quiet
  verify_runtime_env
fi

# redis:7.0.10 自 2026-07-27 起打进 images.tar(现场实测目标机没有该镜像且无网,
# "目标机已有"的假设不成立)。docker load 后统一自检,这里不再前置拦截。
if [ "$LOAD_ONLY" -ne 1 ]; then
  echo "==> 检查已验证的 InfluxDB 目标 ${VALIDATED_INFLUXDB_HOST}:${VALIDATED_INFLUXDB_PORT}"
  if command -v curl >/dev/null 2>&1; then
    INFLUX_HEALTH_URL="http://${VALIDATED_INFLUXDB_HOST}:${VALIDATED_INFLUXDB_PORT}/health"
    curl -fsS --max-time 3 "$INFLUX_HEALTH_URL" >/dev/null 2>&1 \
      && echo "   [OK] ${VALIDATED_INFLUXDB_HOST}:${VALIDATED_INFLUXDB_PORT} 可达" \
      || echo "   [!] ${VALIDATED_INFLUXDB_HOST}:${VALIDATED_INFLUXDB_PORT} 暂不可达 —— 确认现场 InfluxDB 已启动"
  fi
  verify_runtime_env
fi

# 紧贴 docker load 执行，缩小校验后替换镜像包的竞态窗口。
[ -x ./verify-image-archive.py ] || {
  echo "!! 缺少可执行的 ./verify-image-archive.py，离线包不完整" >&2; exit 2
}
echo "==> 校验 images.tar 必备校验和、精确标签与 linux/amd64 架构"
python3 -I ./verify-image-archive.py images.tar images.tar.sha256

echo "==> docker load < images.tar"
docker load -i images.tar

echo "==> 加载后镜像自检（精确标签 + linux/amd64）"
for img in edge-iot/backend:offline-amd64 edge-iot/web:offline-amd64 redis:7.0.10; do
  docker image inspect "$img" >/dev/null 2>&1 \
    || { echo "!! 加载后缺少预期镜像: $img" >&2; exit 1; }
  arch=$(docker image inspect "$img" --format '{{.Architecture}}')
  image_os=$(docker image inspect "$img" --format '{{.Os}}')
  [ "$arch" = "amd64" ] && [ "$image_os" = "linux" ] \
    || { echo "!! 镜像 $img 实际为 ${image_os}/${arch}，拒绝继续" >&2; exit 1; }
  printf '   %-44s %s/%s\n' "$img" "$image_os" "$arch"
done

[ "$LOAD_ONLY" -eq 1 ] && { echo "load-only 完成。"; exit 0; }

# 从此处开始会删/停运行中服务；必须再次确认快照未被替换或改写。
verify_runtime_env

# 固定容器名(redis-iot 等)是全局唯一的:换个目录重新部署时,旧目录起的同名
# 容器会让 up 直接冲突失败(现场实测)。up 前一律清掉同名旧容器 —— 配置数据在
# named volume 里不受影响;正在跑的采集会话被打断后,worker 重启时自动恢复
# RUNNING 会话,不丢状态。注意 volume 跟着 compose 项目名(=目录名)走,换目录
# 部署等于新数据库,起完记得重跑 ./import-and-start.sh(幂等)。
echo "==> 清理旧容器(固定容器名,重复部署冲突预防)"
for c in redis-iot migrate-iot django-iot celery-acq celery-short frontend-iot; do
  if docker ps -a --format '{{.Names}}' | grep -qx "$c"; then
    docker rm -f "$c" >/dev/null 2>&1 && echo "   已移除旧容器 $c"
  fi
done

# 旧版单体系统必须先让位:它与本系统用同一个账号连 SCADA broker,平台单会话
# 限制下两边会 142「会话被接管」互踢,采集断断续续(muju 现场实锤:一个跑了
# 10 天的旧 edge_iot 容器)。stop + restart=no 而非 rm:容器保留可回滚
# (docker update --restart=unless-stopped <名> && docker start <名>),
# 且机器重启后也不会自己复活回来抢会话。旧容器名单只能在受保护 env 中配置。
echo "==> 停用旧版采集系统(同账号会话互踢预防)"
for c in $VALIDATED_LEGACY_CONTAINERS; do
  if docker ps --format '{{.Names}}' | grep -qx "$c"; then
    docker update --restart=no "$c" >/dev/null 2>&1 || true
    docker stop "$c" >/dev/null 2>&1 \
      && echo "   已停用旧版容器 $c(保留未删;回滚: docker update --restart=unless-stopped $c && docker start $c)"
  elif docker ps -a --format '{{.Names}}' | grep -qx "$c"; then
    docker update --restart=no "$c" >/dev/null 2>&1 || true
    echo "   旧版容器 $c 已是停止状态(已禁用其自启)"
  fi
done
# 应急直采脚本(scada_collect.py)残留进程同样抢会话,一并清。
if pgrep -f "scada_collect" >/dev/null 2>&1; then
  pkill -f "scada_collect" 2>/dev/null || true
  echo "   已停止残留的 scada_collect 应急直采脚本"
fi

echo "==> 启动模式：${MODE}"
echo "==> ${DC[*]} ${PROFILE_ARGS[*]} up -d"
verify_runtime_env
run_compose "${ENV_ARGS[@]}" "${PROFILE_ARGS[@]}" -f docker-compose.yml up -d "${UP_ARGS[@]}"
echo ""
run_compose "${ENV_ARGS[@]}" "${PROFILE_ARGS[@]}" -f docker-compose.yml ps
echo ""
if [ ${#PROFILE_ARGS[@]} -gt 0 ]; then
  echo "==> 健康检查：稍候片刻后访问  http://<本机IP>:${VALIDATED_WEB_PORT}/   应返回前端页面"
  echo "    配置完成后可停界面节省资源： docker stop django-iot frontend-iot"
else
  echo "==> 采集模式已启动（无界面）。采集在 celery-acq 中运行，会自动恢复上次 RUNNING 的会话。"
  echo "    需要配置时开界面： ./load-and-up.sh --env-file $ENV_FILE_DISPLAY --ui"
fi
echo "    查看采集日志： docker logs celery-acq --tail 50"
