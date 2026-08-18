#!/usr/bin/env bash
# Validate the protected env file used by the offline production bundle.
# Values are deliberately never echoed: this script is safe to use in CI/field logs.
set -euo pipefail

fail() {
  printf '!! 离线部署配置校验失败: %s\n' "$1" >&2
  exit 2
}

usage() {
  echo "用法: $0 <protected-env-file>" >&2
  exit 2
}

[ "$#" -eq 1 ] || usage
ENV_FILE=$1

[ ! -L "$ENV_FILE" ] || fail "env 文件不能是软链接"
[ -f "$ENV_FILE" ] || fail "env 文件不存在或不是普通文件"
[ -r "$ENV_FILE" ] || fail "env 文件不可读"

# GNU stat on the target Linux host; BSD stat keeps local/macOS checks usable.
if FILE_MODE=$(stat -c '%a' -- "$ENV_FILE" 2>/dev/null) \
    && FILE_OWNER=$(stat -c '%u' -- "$ENV_FILE" 2>/dev/null); then
  :
elif FILE_MODE=$(stat -f '%Lp' -- "$ENV_FILE" 2>/dev/null) \
    && FILE_OWNER=$(stat -f '%u' -- "$ENV_FILE" 2>/dev/null); then
  :
else
  fail "无法读取 env 文件权限"
fi
case "$FILE_MODE" in
  400|600) ;;
  *) fail "env 文件权限必须为 0400 或 0600（当前为 ${FILE_MODE}）" ;;
esac
[ "$FILE_OWNER" = "$(id -u)" ] \
  || fail "env 文件 owner 必须是当前执行用户"

if awk '$0 ~ /^[[:space:]]*(export[[:space:]]+)?COMPOSE_/ { found = 1 } END { exit !found }' "$ENV_FILE"; then
  fail "env 文件禁止定义 COMPOSE_* 控制键"
fi

# Compose accepts several env-file dialect features (notably `export`, quotes
# and ${...} interpolation). Accept only one unambiguous KEY=VALUE spelling for
# the exact keys consumed by this bundle/loader. This prevents an otherwise
# valid-looking file from importing caller-shell values after validation.
if ! awk '
  function allowed(key) {
    return key ~ /^(DEBUG|SECRET_KEY|ALLOWED_HOSTS|ALLOW_PRIVATE_NETWORK_TESTS|OFFLINE_PRIVATE_NETWORK_TESTS_ACK|WEB_PORT|CELERY_ACQ_CONCURRENCY|CELERY_SHORT_CONCURRENCY|REDIS_HOST|REDIS_PORT|LEGACY_CONTAINERS|INFLUXDB_HOST|INFLUXDB_PORT|INFLUXDB_ORG|INFLUXDB_BUCKET|INFLUXDB_TOKEN|INFLUX_SPILL_DB_PATH|DJANGO_DB_NAME|BACKEND_UPSTREAM)$/
  }
  {
    line = $0
    sub(/\r$/, "", line)
    if (line == "" || line ~ /^#/) next
    if (line !~ /^[A-Z][A-Z0-9_]*=/) exit 2
    key = line
    sub(/=.*/, "", key)
    if (!allowed(key) || ++seen[key] != 1) exit 2
    value = substr(line, index(line, "=") + 1)
    if (index(value, "$") || index(value, "`") || index(value, "\\") ||
        index(value, sprintf("%c", 39)) || index(value, sprintf("%c", 34))) exit 2
  }
' "$ENV_FILE"; then
  fail "env 文件含不支持的语法、键、重复定义或变量插值"
fi

value_for() {
  local key=$1 count value
  count=$(awk -v wanted="$key" '
    index($0, wanted "=") == 1 { count += 1 }
    END { print count + 0 }
  ' "$ENV_FILE")
  [ "$count" -eq 1 ] || fail "$key 必须且只能定义一次"
  value=$(awk -v wanted="$key" '
    index($0, wanted "=") == 1 {
      print substr($0, length(wanted) + 2)
    }
  ' "$ENV_FILE")
  value=${value%$'\r'}
  printf '%s' "$value"
}

optional_value_for() {
  local key=$1 count value
  count=$(awk -v wanted="$key" '
    index($0, wanted "=") == 1 { count += 1 }
    END { print count + 0 }
  ' "$ENV_FILE")
  [ "$count" -le 1 ] || fail "$key 只能定义一次"
  [ "$count" -eq 1 ] || { printf '%s' ''; return; }
  value=$(awk -v wanted="$key" '
    index($0, wanted "=") == 1 {
      print substr($0, length(wanted) + 2)
    }
  ' "$ENV_FILE")
  value=${value%$'\r'}
  printf '%s' "$value"
}

require_nonempty() {
  local key=$1 value
  value=$(value_for "$key")
  [ -n "$value" ] || fail "$key 不能为空"
  case "$value" in
    *[[:space:]]*) fail "$key 不能包含空白字符" ;;
    \"*|\'*|*\"|*\') fail "$key 请使用不带引号的值" ;;
  esac
  printf '%s' "$value"
}

reject_placeholder() {
  local key=$1 value=$2 lower
  lower=$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')
  case "$lower" in
    *change-me*|*changeme*|*placeholder*|*replace-me*|*replaceme*|*example*|*default*|*dummy*)
      fail "$key 仍是占位值"
      ;;
  esac
}

require_mixed_long_secret() {
  local key=$1 minimum=$2 value
  value=$(require_nonempty "$key")
  [ "${#value}" -ge "$minimum" ] || fail "$key 长度不能少于 $minimum 个字符"
  reject_placeholder "$key" "$value"
  case "$value" in
    *[!A-Za-z0-9._~+/=-]*) fail "$key 包含不适合 Compose env 文件的字符" ;;
  esac
  case "$value" in *[[:alpha:]]*) ;; *) fail "$key 必须包含字母" ;; esac
  case "$value" in *[[:digit:]]*) ;; *) fail "$key 必须包含数字" ;; esac
}

require_port() {
  local key=$1 value
  value=$(require_nonempty "$key")
  case "$value" in *[!0-9]*) fail "$key 必须是 1~65535 的整数" ;; esac
  [ "$value" -ge 1 ] 2>/dev/null && [ "$value" -le 65535 ] 2>/dev/null \
    || fail "$key 必须是 1~65535 的整数"
}

require_bounded_integer() {
  local key=$1 minimum=$2 maximum=$3 value
  value=$(require_nonempty "$key")
  case "$value" in *[!0-9]*) fail "$key 必须是 $minimum~$maximum 的整数" ;; esac
  [ "$value" -ge "$minimum" ] 2>/dev/null && [ "$value" -le "$maximum" ] 2>/dev/null \
    || fail "$key 必须是 $minimum~$maximum 的整数"
}

require_host() {
  local key=$1 value
  value=$(require_nonempty "$key")
  case "$value" in
    *[!A-Za-z0-9._:\[\]-]*) fail "$key 包含非法字符" ;;
  esac
  case "$value" in
    *:*) case "$value" in \[*\]) ;; *) fail "IPv6 $key 必须使用方括号" ;; esac ;;
  esac
}

validate_container_list() {
  local key=$1 value item count=0
  value=$(value_for "$key")
  # Empty is fail-safe: stop no legacy container unless the operator explicitly
  # lists audited names in the protected file.
  [ -n "$value" ] || return 0
  case "$value" in *$'\t'*|*$'\r'*) fail "$key 只允许用单个空格分隔容器名" ;; esac
  case "$value" in ' '*|*' '|*'  '*) fail "$key 只允许用单个空格分隔容器名" ;; esac
  for item in $value; do
    case "$item" in
      [A-Za-z0-9]*) ;;
      *) fail "$key 包含非法容器名" ;;
    esac
    case "$item" in
      *[!A-Za-z0-9_.-]*) fail "$key 包含非法容器名" ;;
    esac
    count=$((count + 1))
    [ "$count" -le 20 ] || fail "$key 最多允许 20 个容器名"
  done
}

DEBUG_VALUE=$(require_nonempty DEBUG)
case "$DEBUG_VALUE" in
  False|false|0|No|no|Off|off) ;;
  *) fail "DEBUG 在离线生产包中必须为 False" ;;
esac

require_mixed_long_secret SECRET_KEY 50

ALLOWED_HOSTS_VALUE=$(require_nonempty ALLOWED_HOSTS)
case "$ALLOWED_HOSTS_VALUE" in
  *'*'*) fail "ALLOWED_HOSTS 禁止使用通配符 *" ;;
esac

PRIVATE_TESTS_VALUE=$(require_nonempty ALLOW_PRIVATE_NETWORK_TESTS)
case "$PRIVATE_TESTS_VALUE" in
  False|false|0|No|no|Off|off) ;;
  True|true|1|Yes|yes|On|on)
    ACK_VALUE=$(optional_value_for OFFLINE_PRIVATE_NETWORK_TESTS_ACK)
    [ "$ACK_VALUE" = "I_UNDERSTAND_PRIVATE_NETWORK_TESTS_DISABLE_SSRF_PROTECTION" ] \
      || fail "开启私网连接测试前必须显式确认 SSRF 风险"
    ;;
  *) fail "ALLOW_PRIVATE_NETWORK_TESTS 必须为 True 或 False" ;;
esac

require_nonempty INFLUXDB_ORG >/dev/null
require_nonempty INFLUXDB_BUCKET >/dev/null
require_mixed_long_secret INFLUXDB_TOKEN 32
require_host INFLUXDB_HOST
require_port INFLUXDB_PORT
require_host REDIS_HOST
require_port REDIS_PORT
require_port WEB_PORT
require_bounded_integer CELERY_ACQ_CONCURRENCY 1 1024
require_bounded_integer CELERY_SHORT_CONCURRENCY 1 1024
validate_container_list LEGACY_CONTAINERS

echo "[OK] 离线部署 env 文件通过安全校验（未输出敏感值）"
