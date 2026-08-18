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
if FILE_MODE=$(stat -c '%a' -- "$ENV_FILE" 2>/dev/null); then
  :
elif FILE_MODE=$(stat -f '%Lp' -- "$ENV_FILE" 2>/dev/null); then
  :
else
  fail "无法读取 env 文件权限"
fi
case "$FILE_MODE" in
  400|600) ;;
  *) fail "env 文件权限必须为 0400 或 0600（当前为 ${FILE_MODE}）" ;;
esac

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
  case "$value" in *[[:alpha:]]*) ;; *) fail "$key 必须包含字母" ;; esac
  case "$value" in *[[:digit:]]*) ;; *) fail "$key 必须包含数字" ;; esac
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

echo "[OK] 离线部署 env 文件通过安全校验（未输出敏感值）"
