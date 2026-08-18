# M0 备份、恢复与旧镜像回滚手册

更新时间：2026-08-17（Asia/Shanghai）

本手册是 Vue 3/FastAPI 改写前的恢复安全网。所有数据恢复都先写入**全新数据库文件或
全新 Influx bucket**，旧镜像按 immutable ID 归档并绑定；校验通过后再切换配置。不覆盖事实源，不调用
`reset_influxdb.sh`，也不执行 volume/bucket/image prune。

## 1. 强制安全规则

1. 在独立演练机或维护窗口执行；记录操作者、基线 SHA、开始/结束时间和命令输出。
2. 每个路径、org、bucket、镜像 immutable ID 都必须显式填写。不要把 `/`、HOME、仓库根或
   Docker 根目录作为备份目标。
3. Influx token 只放在权限 `0400` 或 `0600` 的独立文件中。不得写入命令行、文档、Git、
   manifest 或聊天记录。
4. SQLite 在线备份只能使用 `scripts/backup/sqlite_backup.py` 的
   `sqlite3.Connection.backup`；WAL 模式下禁止在线 `cp db.sqlite3`。
5. SQLite restore 拒绝已有目标；Influx restore 拒绝源 bucket 和已有 bucket；旧
   镜像只按已核对的 immutable ID 归档和回滚。任何校验失败都停止切换并保留原服务。
6. Redis/Celery 队列和 WebSocket 消息不是事实源，不纳入恢复点。切换前先停采集，
   切换后由既有会话恢复逻辑重新调度。

## 2. 预检与证据目录

以下示例中的值必须由操作者核对后填写，不要直接照抄现场未知值：

```bash
set -euo pipefail
umask 077
export RECOVERY_ID='m0-20260817T120000-0800'
export EVIDENCE_ROOT="/srv/edge-iot-recovery/${RECOVERY_ID}"
export COMPOSE_FILE="$(pwd -P)/deploy/offline/docker-compose.offline.yml"
export COMPOSE_ENV_FILE='/etc/edge-iot/production.env'
export EDGE_WRITER_CONTAINER='celery-acq'
export INFLUX_HOST='http://127.0.0.1:8086'
export INFLUX_ORG='Midea'
export INFLUX_BUCKET='Record'
export INFLUX_TOKEN_FILE='/run/secrets/edge-iot-influx.token'

test -n "$RECOVERY_ID"
test -n "$EVIDENCE_ROOT"
test -f "$COMPOSE_FILE"
case "$COMPOSE_ENV_FILE" in /*) ;; *) echo 'COMPOSE_ENV_FILE 必须是绝对路径' >&2; exit 2 ;; esac
test ! -L "$COMPOSE_ENV_FILE"
test -f "$COMPOSE_ENV_FILE"
case "$INFLUX_TOKEN_FILE" in /*) ;; *) echo 'INFLUX_TOKEN_FILE 必须是绝对路径' >&2; exit 2 ;; esac
test ! -L "$INFLUX_TOKEN_FILE"
test -f "$INFLUX_TOKEN_FILE"

if stat --version >/dev/null 2>&1; then
  TOKEN_MODE="$(stat -c '%a' -- "$INFLUX_TOKEN_FILE")"
  TOKEN_OWNER="$(stat -c '%u' -- "$INFLUX_TOKEN_FILE")"
  COMPOSE_ENV_MODE="$(stat -c '%a' -- "$COMPOSE_ENV_FILE")"
  COMPOSE_ENV_OWNER="$(stat -c '%u' -- "$COMPOSE_ENV_FILE")"
else
  TOKEN_MODE="$(stat -f '%Lp' "$INFLUX_TOKEN_FILE")"
  TOKEN_OWNER="$(stat -f '%u' "$INFLUX_TOKEN_FILE")"
  COMPOSE_ENV_MODE="$(stat -f '%Lp' "$COMPOSE_ENV_FILE")"
  COMPOSE_ENV_OWNER="$(stat -f '%u' "$COMPOSE_ENV_FILE")"
fi
case "$TOKEN_MODE" in
  400|600) ;;
  *) echo "token 文件权限必须是 0400 或 0600，当前为 $TOKEN_MODE" >&2; exit 2 ;;
esac
case "$COMPOSE_ENV_MODE" in
  400|600) ;;
  *) echo "Compose env 文件权限必须是 0400 或 0600，当前为 $COMPOSE_ENV_MODE" >&2; exit 2 ;;
esac
test "$TOKEN_OWNER" = "$(id -u)"
test "$COMPOSE_ENV_OWNER" = "$(id -u)"

# 不猜 project/volume 名：从当前采集容器的 Compose 标签与 /data mount 反查，
# 再用 volume 标签做双向核对。任何标签为空或不一致都停止。
COMPOSE_PROJECT="$(docker inspect "$EDGE_WRITER_CONTAINER" \
  --format '{{ index .Config.Labels "com.docker.compose.project" }}')"
WRITER_SERVICE="$(docker inspect "$EDGE_WRITER_CONTAINER" \
  --format '{{ index .Config.Labels "com.docker.compose.service" }}')"
APP_DB_VOLUME="$(docker inspect "$EDGE_WRITER_CONTAINER" \
  --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}')"
test -n "$COMPOSE_PROJECT"
test "$WRITER_SERVICE" = 'celery-acq'
test -n "$APP_DB_VOLUME"

# Config.Env 是 JSON string array。按完整前缀切片，不用 cut/split('=')，因此其它
# 环境项或值里即使含 '=' 也不会误解析。Python 在输出前完成唯一性和路径白名单校验。
DJANGO_DB_NAME="$(
  docker inspect "$EDGE_WRITER_CONTAINER" --format '{{json .Config.Env}}' \
  | python3 -c '
import json
import re
import sys

entries = json.load(sys.stdin)
prefix = "DJANGO_DB_NAME="
matches = [
    entry[len(prefix):]
    for entry in entries
    if isinstance(entry, str) and entry.startswith(prefix)
]
if len(matches) != 1:
    raise SystemExit("必须且只能解析到一个 DJANGO_DB_NAME")
value = matches[0]
if not value or any(ord(character) < 32 or ord(character) == 127 for character in value):
    raise SystemExit("DJANGO_DB_NAME 含空值或控制字符")
parts = value.split("/")
if len(parts) < 3 or parts[:2] != ["", "data"]:
    raise SystemExit("DJANGO_DB_NAME 必须是 /data 下的绝对文件路径")
safe_component = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
if any(part in {"", ".", ".."} or safe_component.fullmatch(part) is None for part in parts[2:]):
    raise SystemExit("DJANGO_DB_NAME 含不安全的相对路径组件")
print(value)
'
)"
test -n "$DJANGO_DB_NAME"
DJANGO_DB_DIR="${DJANGO_DB_NAME%/*}"
test -n "$DJANGO_DB_DIR"

VOLUME_PROJECT="$(docker volume inspect "$APP_DB_VOLUME" \
  --format '{{ index .Labels "com.docker.compose.project" }}')"
VOLUME_LOGICAL_NAME="$(docker volume inspect "$APP_DB_VOLUME" \
  --format '{{ index .Labels "com.docker.compose.volume" }}')"
test "$VOLUME_PROJECT" = "$COMPOSE_PROJECT"
test "$VOLUME_LOGICAL_NAME" = 'app-db'
export COMPOSE_PROJECT COMPOSE_PROJECT_NAME="$COMPOSE_PROJECT" APP_DB_VOLUME
export DJANGO_DB_NAME DJANGO_DB_DIR

# 不把 compose config 或容器环境写盘/tee：其中含 SECRET_KEY 和 Influx token。
# 对本次可能重建的全部 6 个服务逐一比较：image Config.Env 与 Compose environment
# 合并后的完整 effective env 必须精确等于容器 Config.Env，effective command 必须等于
# Config.Cmd，容器顶层 immutable Image ID 必须等于当前 rendered tag 的镜像 ID。循环会
# 自动纳入未来新增的 INFLUX_SPILL_DB_PATH 等字段；stdout 只含非敏感镜像身份报告。
STACK_IMAGE_REPORT="$(
python3 - "$COMPOSE_ENV_FILE" "$COMPOSE_PROJECT" "$COMPOSE_FILE" \
  "$INFLUX_TOKEN_FILE" "$INFLUX_HOST" "$INFLUX_ORG" "$INFLUX_BUCKET" <<'PY'
import json
import os
import re
import shlex
import subprocess
import sys

(
    env_file,
    project,
    compose_file,
    token_file,
    explicit_host,
    explicit_org,
    explicit_bucket,
) = sys.argv[1:]
services = ("redis", "migrate", "celery-acq", "celery-short", "django", "web")
compose_prefix = [
    "docker", "compose", "--env-file", env_file,
    "-p", project, "-f", compose_file,
]

def run_text(command, description):
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        raise SystemExit(f"{description}失败；输出可能含敏感值，未打印")
    return completed.stdout

rendered = json.loads(
    run_text(compose_prefix + ["config", "--format", "json"], "Compose render")
)
rendered_services = rendered.get("services")
if not isinstance(rendered_services, dict):
    raise SystemExit("Compose render 缺少 services")

container_ids = {}
missing = []
for service in services:
    if service not in rendered_services:
        raise SystemExit(f"Compose render 缺少待重建服务: {service}")
    identifiers = [
        line for line in run_text(
            compose_prefix + ["ps", "-aq", service],
            f"查询 {service} 容器",
        ).splitlines() if line
    ]
    if len(identifiers) != 1:
        missing.append(service)
    else:
        container_ids[service] = identifiers[0]
if missing:
    raise SystemExit(
        "缺少或无法唯一定位当前容器: " + ",".join(missing)
        + "；预检必须失败。须由现场负责人显式批准、形成 0600 非 Git 证据，"
          "并重新建立可逐项对账的容器基线后再执行本手册"
    )

def parse_environment(entries, service):
    environment = {}
    for entry in entries or []:
        key, separator, value = entry.partition("=")
        if not separator or key in environment:
            raise SystemExit(f"{service} 当前 Config.Env 格式异常或存在重复 key")
        environment[key] = value
    return environment

def normalized_command(value, service):
    if value is None:
        return []
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    if isinstance(value, str):
        return shlex.split(value)
    raise SystemExit(f"{service} command 格式无法安全对账")

actual_environments = {}
matched_counts = {}
actual_image_ids = {}
rendered_images = {}
for service in services:
    container_document = json.loads(
        run_text(
            [
                "docker", "inspect", container_ids[service],
                "--format", "{{json .}}",
            ],
            f"读取 {service} 容器身份与 Config",
        )
    )
    container_config = container_document.get("Config")
    if not isinstance(container_config, dict):
        raise SystemExit(f"{service} 容器缺少 Config")
    service_definition = rendered_services[service]
    rendered_environment = service_definition.get("environment") or {}
    if not isinstance(rendered_environment, dict):
        raise SystemExit(f"{service} rendered environment 不是键值对象")

    rendered_image = service_definition.get("image")
    if not isinstance(rendered_image, str) or not rendered_image:
        raise SystemExit(f"{service} 缺少显式 image")
    if container_config.get("Image") != rendered_image:
        raise SystemExit(f"{service} 当前容器与 Compose image 引用不一致")
    image_document = json.loads(
        run_text(
            [
                "docker", "image", "inspect", rendered_image,
                "--format", "{{json .}}",
            ],
            f"读取 {service} rendered image",
        )
    )
    immutable_image_id = image_document.get("Id")
    if (
        not isinstance(immutable_image_id, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", immutable_image_id) is None
    ):
        raise SystemExit(f"{service} rendered image 缺少合法 immutable ID")
    if container_document.get("Image") != immutable_image_id:
        raise SystemExit(
            f"{service} 运行容器 immutable Image ID 与当前 rendered tag 不一致"
        )
    image_config = image_document.get("Config")
    if not isinstance(image_config, dict):
        raise SystemExit(f"{service} rendered image 缺少 Config")

    expected_environment = parse_environment(
        image_config.get("Env"), service + " image"
    )
    for key, rendered_value in rendered_environment.items():
        if rendered_value is None:
            expected_environment.pop(key, None)
        else:
            expected_environment[key] = str(rendered_value)
    actual_environment = parse_environment(container_config.get("Env"), service)
    if expected_environment != actual_environment:
        differing_keys = sorted(
            set(expected_environment) ^ set(actual_environment)
            | {
                key
                for key in set(expected_environment) & set(actual_environment)
                if expected_environment[key] != actual_environment[key]
            }
        )
        raise SystemExit(
            f"{service} effective environment 与当前 Config.Env 不一致；"
            "仅列键名: " + ",".join(differing_keys)
        )

    if "command" in service_definition and service_definition["command"] is not None:
        expected_command = normalized_command(service_definition["command"], service)
    else:
        expected_command = normalized_command(image_config.get("Cmd"), service)
    actual_command = normalized_command(container_config.get("Cmd"), service)
    if expected_command != actual_command:
        raise SystemExit(f"{service} rendered effective command 与当前 Config.Cmd 不一致")
    actual_environments[service] = actual_environment
    matched_counts[service] = len(expected_environment)
    actual_image_ids[service] = immutable_image_id
    rendered_images[service] = rendered_image

container_environment = actual_environments["celery-acq"]
for required_key in (
    "INFLUXDB_HOST", "INFLUXDB_PORT", "INFLUXDB_TOKEN",
    "INFLUXDB_ORG", "INFLUXDB_BUCKET",
):
    if required_key not in container_environment:
        raise SystemExit(f"celery-acq 缺少 Influx scope 键: {required_key}")

token_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
token_descriptor = os.open(token_file, token_flags)
try:
    token_chunks = []
    token_size = 0
    while token_size <= 65536:
        chunk = os.read(token_descriptor, min(8192, 65537 - token_size))
        if not chunk:
            break
        token_chunks.append(chunk)
        token_size += len(chunk)
finally:
    os.close(token_descriptor)
token = b"".join(token_chunks)
if token_size > 65536 or token.decode("utf-8").strip() != container_environment["INFLUXDB_TOKEN"]:
    raise SystemExit("INFLUX token 文件与当前 writer 不一致")
expected_host = "http://{}:{}".format(
    container_environment["INFLUXDB_HOST"],
    container_environment["INFLUXDB_PORT"],
)
if explicit_host.rstrip("/") != expected_host:
    raise SystemExit("显式 INFLUX_HOST 与当前 writer 不一致")
if explicit_org != container_environment["INFLUXDB_ORG"]:
    raise SystemExit("显式 INFLUX_ORG 与当前 writer 不一致")
if explicit_bucket != container_environment["INFLUXDB_BUCKET"]:
    raise SystemExit("显式 INFLUX_BUCKET 与当前 writer 不一致")
print("service\trendered_image\timmutable_image_id\teffective_env_keys")
for service in services:
    print(
        "{}\t{}\t{}\t{}".format(
            service,
            rendered_images[service],
            actual_image_ids[service],
            matched_counts[service],
        )
    )
PY
)"

test ! -e "$EVIDENCE_ROOT"
install -d -m 0700 "$EVIDENCE_ROOT"
printf '%s\n' "$STACK_IMAGE_REPORT" > "$EVIDENCE_ROOT/stack-images.tsv"
chmod 0600 "$EVIDENCE_ROOT/stack-images.tsv"
printf 'compose_project=%s\nwriter_service=%s\napp_db_volume=%s\ndjango_db_name=%s\n' \
  "$COMPOSE_PROJECT" "$WRITER_SERVICE" "$APP_DB_VOLUME" "$DJANGO_DB_NAME" \
  | tee "$EVIDENCE_ROOT/stack-identity.txt"
git rev-parse HEAD | tee "$EVIDENCE_ROOT/source.sha"
python3 --version 2>&1 | tee "$EVIDENCE_ROOT/python-version.txt"
influx version 2>&1 | tee "$EVIDENCE_ROOT/influx-cli-version.txt"
docker compose --env-file "$COMPOSE_ENV_FILE" \
  -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" ps \
  | tee "$EVIDENCE_ROOT/compose-before.txt"
docker volume inspect "$APP_DB_VOLUME" \
  | tee "$EVIDENCE_ROOT/app-db-volume.json"
```

若容器或标签核对失败，先确认当前运行实例，不能用目录名、通配符或手填默认值猜
Compose project/volume。后续每条 Compose 命令都必须同时带
`--env-file "$COMPOSE_ENV_FILE" -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE"`；仅设置
环境变量不能代替命令级显式绑定。`COMPOSE_ENV_FILE` 含现场密钥，只能保存在受保护的
绝对路径，不得复制进证据目录、终端日志或 Git；上面的对账不输出任何环境值，只把
服务名、rendered image 引用、实际 immutable ID 和 effective env 键数写入证据。
证据命令继承 `umask 077`，文件默认为 `0600`，整个证据目录也不得放进 Git。

## 3. SQLite WAL 一致性备份

### 3.1 在线备份

此命令把工具只读挂载进一次性容器，并把 SQLite 备份写到新证据目录。应用可以保持
运行；SQLite backup API 会取得一致快照。工具先在目标同目录创建权限 `0600` 的随机
临时文件，并从创建起持续持有该 fd 和父目录 dirfd。SQLite 连接建立后、执行首条
SQL/PRAGMA 前，工具在 Linux `/proc/self/fd` 或 macOS `/dev/fd` 中要求唯一新增的
SQLite 主 fd 与临时 fd 的 device/inode 相同；随后 backup、`integrity_check` 和关键表
检查复用该连接。连接关闭后通过持有的 fd 执行 SHA-256/`fsync`，Linux 用
`linkat(fd, AT_EMPTY_PATH)`（必要时只回退到 `/proc/self/fd/N`）发布，macOS 用
`fclonefileat(fd, ...)` 发布。两者都不覆盖已有目标，也不再解析临时路径作为发布来源。
为消除 `lstat→unlink` 的同 UID 竞态，工具在成功和失败时都**不自动删除任何临时
文件**；成功报告给出临时路径提示及其 device/inode，正式目标只保证摘要相同。

```bash
export SQLITE_BACKUP_NAME="db-${RECOVERY_ID}.sqlite3"
export SQLITE_BACKUP_UID="$(id -u)"
export SQLITE_BACKUP_GID="$(id -g)"

# backend 镜像默认 root，但 /backup 是宿主操作者创建的 0700/uid 目录。备份容器必须
# 显式用该 uid/gid；先以完全相同身份验证 DB 及存在的 WAL/SHM 都是普通、非 symlink
# 且可读。失败时停止，不自动 chmod/chown 生产卷。
docker run --rm --user "${SQLITE_BACKUP_UID}:${SQLITE_BACKUP_GID}" \
  --mount "type=volume,src=${APP_DB_VOLUME},dst=/data,readonly" \
  edge-iot/backend:offline-amd64 \
  python -c '
import os
import stat
import sys

database = sys.argv[1]
for candidate in (database, database + "-wal", database + "-shm"):
    if candidate != database and not os.path.lexists(candidate):
        continue
    info = os.lstat(candidate)
    if not stat.S_ISREG(info.st_mode) or not os.access(candidate, os.R_OK):
        raise SystemExit("数据库/WAL/SHM 对备份 uid 不可安全读取: " + candidate)
' "$DJANGO_DB_NAME"

docker run --rm \
  --user "${SQLITE_BACKUP_UID}:${SQLITE_BACKUP_GID}" \
  --mount "type=volume,src=${APP_DB_VOLUME},dst=/data,readonly" \
  --mount "type=bind,src=$(pwd)/scripts/backup/sqlite_backup.py,dst=/tool/sqlite_backup.py,readonly" \
  --mount "type=bind,src=${EVIDENCE_ROOT},dst=/backup" \
  edge-iot/backend:offline-amd64 \
  python /tool/sqlite_backup.py backup \
    --source "$DJANGO_DB_NAME" \
    --destination "/backup/${SQLITE_BACKUP_NAME}" \
  | tee "$EVIDENCE_ROOT/sqlite-backup-report.json"
```

验收条件：命令退出码为 0，报告中 `integrity_check` 为 `ok`，关键表计数与现场规模
相符，并且 `missing_key_tables` 为空。缺关键表默认直接失败；只有应用负责人确认并
记录旧 schema 后，才可显式加 `--allow-missing-key-tables` 生成兼容性备份，此类备份
不得自动作为迁移验收恢复点。所有选定关键表都为 0 行时也默认失败；只有确认这是
预期空配置时才可显式加 `--allow-empty-key-tables`。成功后先核对正式目标的
`destination_sha256`，再将报告中的 `retained_temporary_identity` 与临时路径现场
`stat` 对账；Linux 通常是同 inode，macOS clone 是不同 inode、会占额外空间。只有隔离
该私有父目录、确认没有同 UID 并发进程且摘要/identity 都吻合后才人工删除隐藏临时
文件。失败临时文件也只做人工核对，不得用通配符批量删除；若路径已被竞争者替换，
报告路径只可作为定位线索，不能直接作为删除授权。

### 3.2 恢复演练（写新文件）

restore 先检查备份本身，再在 `$DJANGO_DB_DIR` 同目录的 `0600` 随机临时文件中执行 backup
API、完整性检查和逐表计数比较，全部通过后才以 fd-safe、不覆盖方式发布新文件。它
不会覆盖或异常清理 `$DJANGO_DB_NAME`，也不会删除竞争进程先创建的目标。恢复容器保留
镜像默认 root，以便写入 root 所有的 `/data` volume；其临时文件同样只允许人工按
摘要及报告 identity 核对后清理。

```bash
export SQLITE_RESTORE_NAME="db-restored-${RECOVERY_ID}.sqlite3"
docker run --rm \
  --mount "type=volume,src=${APP_DB_VOLUME},dst=/data" \
  --mount "type=bind,src=$(pwd)/scripts/backup/sqlite_backup.py,dst=/tool/sqlite_backup.py,readonly" \
  --mount "type=bind,src=${EVIDENCE_ROOT},dst=/backup,readonly" \
  edge-iot/backend:offline-amd64 \
  python /tool/sqlite_backup.py restore \
    --source "/backup/${SQLITE_BACKUP_NAME}" \
    --destination "${DJANGO_DB_DIR}/${SQLITE_RESTORE_NAME}" \
  | tee "$EVIDENCE_ROOT/sqlite-restore-report.json"
```

验收条件：`source_integrity_check=ok`、`integrity_check=ok`、
`counts_match_source=true`，关键表计数与备份报告一致。通过后才允许在维护窗口把
`$COMPOSE_ENV_FILE` 中 `DJANGO_DB_NAME` 改为
`${DJANGO_DB_DIR}/${SQLITE_RESTORE_NAME}`，保持文件权限 `0400/0600` 且绝不提交 Git，
然后使用同一个显式 env 文件重建后端容器。
原 `$DJANGO_DB_NAME` 保留不动，可通过恢复原环境变量回退。

开发机不用 Docker volume 时执行同一工具即可，但 source/destination 必须是绝对、
精确文件路径：

```bash
python3 scripts/backup/sqlite_backup.py backup \
  --source /absolute/path/to/backend/db.sqlite3 \
  --destination "/absolute/backup/path/db-${RECOVERY_ID}.sqlite3"
```

## 4. InfluxDB 2.x 备份与恢复验证

`scripts/backup/influx_backup.py` 需要 Python 3 和 Influx CLI 2.x。token 通过
单个安全文件描述符读取，再通过 `INFLUX_TOKEN` 子进程环境传递，不出现在参数或
报告中。每条 Influx CLI 命令都有显式有限超时。backup 前后会记录
measurement/field 结构、field-value 数量和 observed first/last 时间范围；这些只是
结构/范围摘要校验，**不能证明逐点值内容相等**。backup CLI 只写最终目标同父目录内
新建的 `0700` 私有 staging 子目录；wrapper 会用不跟随 symlink 的文件描述符遍历，
拒绝 symlink、FIFO、socket、device 等特殊项，完成权限、SHA-256、验证和 `fsync`
后才以平台原子 no-replace rename 发布最终目录。竞争者先占最终路径时绝不覆盖，失败
和成功都不自动删除 staging：失败目录可能含诊断材料，成功后为空目录；报告或错误消息
给出 staging 路径，操作者只能在隔离父目录并核对 inode 后人工清理，禁止通配符删除。

### 4.1 静默写入并备份

当前离线 Compose 中，连续写 Influx 的服务只有 `celery-acq`；`django` 和
`celery-short` 保持原状态。维护
窗口内禁止手工启动采集或调用任何写入型诊断。下面先记录 writer 原状态，只在它原本
运行时停止，并注册 EXIT/信号 trap：无论 backup/verify 成功还是失败都尝试恢复原
状态。不要停 InfluxDB。

```bash
set -euo pipefail
umask 077
export INFLUX_WRITER_SERVICE='celery-acq'
WRITER_CONTAINER_ID="$(docker compose --env-file "$COMPOSE_ENV_FILE" \
  -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" ps -aq "$INFLUX_WRITER_SERVICE")"
test -n "$WRITER_CONTAINER_ID"
test "$(printf '%s\n' "$WRITER_CONTAINER_ID" | wc -l | tr -d ' ')" = '1'
docker inspect "$WRITER_CONTAINER_ID" --format '{{json .State}}' \
  | tee "$EVIDENCE_ROOT/influx-writer-state-before.json"
WRITER_WAS_RUNNING="$(docker inspect "$WRITER_CONTAINER_ID" \
  --format '{{.State.Running}}')"
case "$WRITER_WAS_RUNNING" in true|false) ;; *) exit 2 ;; esac
WRITER_RESTORED=false

restore_influx_writer() {
  if [ "$WRITER_RESTORED" = true ]; then
    return 0
  fi
  WRITER_IS_RUNNING="$(docker inspect "$WRITER_CONTAINER_ID" \
    --format '{{.State.Running}}')"
  if [ "$WRITER_WAS_RUNNING" = true ] && [ "$WRITER_IS_RUNNING" != true ]; then
    if ! docker compose --env-file "$COMPOSE_ENV_FILE" \
      -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" start "$INFLUX_WRITER_SERVICE"
    then
      echo '严重：Influx writer 原状态恢复失败，需要立即人工处理' >&2
      return 1
    fi
  elif [ "$WRITER_WAS_RUNNING" = false ] && [ "$WRITER_IS_RUNNING" != false ]; then
    if ! docker compose --env-file "$COMPOSE_ENV_FILE" \
      -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" \
      stop --timeout 60 "$INFLUX_WRITER_SERVICE"
    then
      echo '严重：Influx writer 原停止状态恢复失败，需要立即人工处理' >&2
      return 1
    fi
  fi
  WRITER_IS_RUNNING="$(docker inspect "$WRITER_CONTAINER_ID" \
    --format '{{.State.Running}}')"
  if [ "$WRITER_IS_RUNNING" != "$WRITER_WAS_RUNNING" ]; then
    echo '严重：Influx writer 恢复后状态仍与原状态不一致' >&2
    return 1
  fi
  sleep 2
  WRITER_STABLE_RUNNING="$(docker inspect "$WRITER_CONTAINER_ID" \
    --format '{{.State.Running}}')"
  if [ "$WRITER_STABLE_RUNNING" != "$WRITER_WAS_RUNNING" ]; then
    echo '严重：Influx writer 恢复后未保持稳定状态' >&2
    return 1
  fi
  docker inspect "$WRITER_CONTAINER_ID" --format '{{json .State}}' \
    > "$EVIDENCE_ROOT/influx-writer-state-after.json"
  WRITER_RESTORED=true
}

trap restore_influx_writer EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if [ "$WRITER_WAS_RUNNING" = true ]; then
  docker compose --env-file "$COMPOSE_ENV_FILE" \
    -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" \
    stop --timeout 60 "$INFLUX_WRITER_SERVICE"
fi
docker inspect "$WRITER_CONTAINER_ID" --format '{{json .State}}' \
  | tee "$EVIDENCE_ROOT/influx-writer-state-during-backup.json"
WRITER_IS_RUNNING="$(docker inspect "$WRITER_CONTAINER_ID" \
  --format '{{.State.Running}}')"
if [ "$WRITER_IS_RUNNING" != false ]; then
  echo '严重：Influx writer 未真正停止，拒绝继续备份' >&2
  exit 2
fi
sleep 2
WRITER_STABLE_RUNNING="$(docker inspect "$WRITER_CONTAINER_ID" \
  --format '{{.State.Running}}')"
if [ "$WRITER_STABLE_RUNNING" != false ]; then
  echo '严重：Influx writer 停止状态不稳定，拒绝继续备份' >&2
  exit 2
fi

export INFLUX_BACKUP_PATH="${EVIDENCE_ROOT}/influx-${RECOVERY_ID}"
python3 scripts/backup/influx_backup.py backup \
  --host "$INFLUX_HOST" \
  --org "$INFLUX_ORG" \
  --bucket "$INFLUX_BUCKET" \
  --token-file "$INFLUX_TOKEN_FILE" \
  --path "$INFLUX_BACKUP_PATH" \
  --command-timeout 3600 \
  | tee "$EVIDENCE_ROOT/influx-backup-report.json"

python3 scripts/backup/influx_backup.py verify-archive \
  --host "$INFLUX_HOST" \
  --org "$INFLUX_ORG" \
  --bucket "$INFLUX_BUCKET" \
  --path "$INFLUX_BACKUP_PATH" \
  | tee "$EVIDENCE_ROOT/influx-archive-verify.json"

restore_influx_writer
trap - EXIT HUP INT TERM
```

验收条件：writer 的 before/after `State.Running` 与原状态一致，报告中
`source_structure_range_stable=true`、`archive_checksums=ok`，并把报告中的
`checksum_document_sha256` 另存到只读介质；同时保存 `series_count`、
`field_value_count` 以及每个 measurement/field 的 first/last。摘要不同则保持原
bucket、不切换，并用**新的备份路径**重试；摘要相同也只代表结构/数量/范围一致，
不能宣称内容级相等。

空 bucket 默认拒绝。只有证据确认它预期为空时，backup、verify 和后续 restore 每一步
都显式加 `--allow-empty-snapshot`；缺少任一步开关都会失败。

### 4.2 恢复到新 bucket 并自动比对

工具只支持同 org 内恢复到新 bucket，不支持 `--full`，也不会删除旧 bucket：
archive 的父目录必须由当前 UID 所有且不可 group/world 写。wrapper 在 checksum 验证时
记录 archive 根 inode，然后通过 `dirfd`/`O_NOFOLLOW` 把所有普通文件复制到新的私有
staging，目录改为 `0500`、文件改为 `0400` 并完成 `fsync`。远端 bucket 存在性检查后
会再次逐文件验证完整 checksum，restore CLI 只读取该副本，结束后再复验。原 archive
被替换不影响已绑定副本；副本内容原地变化会在调用 restore 前失败。成功或失败都保留
`retained_restore_staging_path` 供人工核对，不自动删除。

```bash
export INFLUX_RESTORE_BUCKET="Record_m0_restore_${RECOVERY_ID}"
python3 scripts/backup/influx_backup.py restore \
  --host "$INFLUX_HOST" \
  --org "$INFLUX_ORG" \
  --bucket "$INFLUX_BUCKET" \
  --restore-bucket "$INFLUX_RESTORE_BUCKET" \
  --token-file "$INFLUX_TOKEN_FILE" \
  --path "$INFLUX_BACKUP_PATH" \
  --command-timeout 3600 \
  | tee "$EVIDENCE_ROOT/influx-restore-report.json"
```

验收条件：archive checksum 通过、目标 bucket 事前不存在、
`structure_and_range_match=true`。这仍不是逐点内容校验；关键现场必须另做抽样值/业务
查询核对。只有通过后才允许把受保护的 `$COMPOSE_ENV_FILE` 中 `INFLUXDB_BUCKET` 改成
新 bucket（保持 `0400/0600`，不提交 Git）并用同一 env 文件重启应用。回退时恢复原
bucket 配置；不要 drop 或 reset 任一 bucket。

## 5. 旧应用镜像归档、校验与回滚

### 5.1 在部署新版本前归档

以下命令从 `0600` 的 `stack-images.tsv` 读取并交叉校验当前 immutable image ID，且让
`docker image save` 直接接收三个 ID，不创建任何可变 image tag。tar 先写同父
目录的独占 `0600` staging，验证
identity/摘要并 `fsync` 后，仅从持有的 fd 以 Linux `linkat` 或 macOS `fclonefileat`
原子、不覆盖发布：

```bash
image_id_for_service() {
  awk -F '\t' -v wanted="$1" '$1 == wanted { print $3 }' \
    "$EVIDENCE_ROOT/stack-images.tsv"
}
BACKEND_IMAGE_ID="$(image_id_for_service celery-acq)"
WEB_IMAGE_ID="$(image_id_for_service web)"
REDIS_IMAGE_ID="$(image_id_for_service redis)"
[[ "$BACKEND_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$WEB_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$REDIS_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]]

# 四个 backend 服务必须来自同一份已对账 immutable image；任何分歧都停止归档。
for BACKEND_SERVICE in migrate celery-acq celery-short django; do
  test "$(image_id_for_service "$BACKEND_SERVICE")" = "$BACKEND_IMAGE_ID"
done
test "$(docker image inspect "$BACKEND_IMAGE_ID" --format '{{.Id}}')" = "$BACKEND_IMAGE_ID"
test "$(docker image inspect "$WEB_IMAGE_ID" --format '{{.Id}}')" = "$WEB_IMAGE_ID"
test "$(docker image inspect "$REDIS_IMAGE_ID" --format '{{.Id}}')" = "$REDIS_IMAGE_ID"

export IMAGE_TAR_PATH="$EVIDENCE_ROOT/old-images.tar"
test ! -e "$IMAGE_TAR_PATH"
test ! -L "$IMAGE_TAR_PATH"
# 归档输入直接使用 immutable IDs；不创建任何可变 image tag。
IMAGE_ARCHIVE_REPORT="$(
python3 - "$IMAGE_TAR_PATH" "$BACKEND_IMAGE_ID" "$WEB_IMAGE_ID" \
  "$REDIS_IMAGE_ID" 3600 <<'PY_IMAGE_ARCHIVE'
import ctypes
import errno
import hashlib
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

destination = Path(os.path.abspath(sys.argv[1]))
image_ids = sys.argv[2:5]
timeout = float(sys.argv[5])
if not math.isfinite(timeout) or timeout <= 0:
    raise SystemExit("image save timeout 必须是正有限值")
if any(re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None for value in image_ids):
    raise SystemExit("image save 只接受三个 immutable sha256 ID")
parent_stat = os.lstat(destination.parent)
if (
    not stat.S_ISDIR(parent_stat.st_mode)
    or stat.S_ISLNK(parent_stat.st_mode)
    or parent_stat.st_uid != os.geteuid()
    or stat.S_IMODE(parent_stat.st_mode) & 0o022
):
    raise SystemExit("image tar 父目录不是当前 UID 控制的安全目录")
expected_parent_identity = (parent_stat.st_dev, parent_stat.st_ino)
directory_descriptor = os.open(
    destination.parent,
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0),
)
directory_stat = os.fstat(directory_descriptor)
if (directory_stat.st_dev, directory_stat.st_ino) != expected_parent_identity:
    os.close(directory_descriptor)
    raise SystemExit("image tar 父目录 identity 已变化")
for reserved_name in (destination.name, destination.name + ".sha256"):
    try:
        os.stat(reserved_name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        os.close(directory_descriptor)
        raise SystemExit("拒绝覆盖已有 image tar/checksum")

# tempfile.mkstemp 使用 O_CREAT|O_EXCL；fd 从创建、Docker 写入、hash/fsync 到发布
# 全程保持打开，后续绝不把可替换的 staging pathname 交给 Docker。
descriptor, temporary_name = tempfile.mkstemp(
    dir=str(destination.parent), prefix=".old-images.tar.", suffix=".staging"
)
staging = Path(temporary_name)
os.fchmod(descriptor, 0o600)
staging_stat = os.fstat(descriptor)
expected_identity = (staging_stat.st_dev, staging_stat.st_ino)
current_stage_stat = os.stat(
    staging.name, dir_fd=directory_descriptor, follow_symlinks=False
)
if (
    not stat.S_ISREG(staging_stat.st_mode)
    or (current_stage_stat.st_dev, current_stage_stat.st_ino) != expected_identity
):
    os.close(descriptor)
    os.close(directory_descriptor)
    raise SystemExit("image tar staging 未创建在已持有的父目录")
flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)

def sha256_fd(fd, expected, label):
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or (before.st_dev, before.st_ino) != expected:
        raise SystemExit(label + " identity 不一致")
    digest = hashlib.sha256()
    offset = 0
    while offset < before.st_size:
        chunk = os.pread(fd, min(1024 * 1024, before.st_size - offset), offset)
        if not chunk:
            raise SystemExit(label + " hash 时提前结束")
        digest.update(chunk)
        offset += len(chunk)
    after = os.fstat(fd)
    if (
        (after.st_dev, after.st_ino) != expected
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise SystemExit(label + " hash 期间发生变化")
    return digest.hexdigest()

try:
    try:
        completed = subprocess.run(
            ["docker", "image", "save", *image_ids],
            stdout=descriptor,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("docker image save 超时；stdout/stderr 已抑制") from None
    except OSError:
        raise RuntimeError("无法执行 docker image save；stdout/stderr 已抑制") from None
    if completed.returncode != 0:
        raise RuntimeError("docker image save 失败；stdout/stderr 已抑制")
    opened_stat = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened_stat.st_mode)
        or (opened_stat.st_dev, opened_stat.st_ino) != expected_identity
        or stat.S_IMODE(opened_stat.st_mode) != 0o600
        or opened_stat.st_size <= 0
    ):
        raise RuntimeError("docker image save 未生成安全、非空的 staging tar")
    staging_digest = sha256_fd(descriptor, expected_identity, "image tar staging")
    os.fsync(descriptor)

    current_parent_stat = os.lstat(destination.parent)
    if (
        not stat.S_ISDIR(current_parent_stat.st_mode)
        or (current_parent_stat.st_dev, current_parent_stat.st_ino)
        != expected_parent_identity
    ):
        raise RuntimeError("image tar 父目录在 save 期间发生变化")

    library = ctypes.CDLL(None, use_errno=True)
    destination_bytes = os.fsencode(destination.name)
    if sys.platform.startswith("linux"):
        linkat = library.linkat
        linkat.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        linkat.restype = ctypes.c_int
        result = linkat(descriptor, b"", directory_descriptor, destination_bytes, 0x1000)
        if result != 0:
            first_error = ctypes.get_errno()
            if first_error == errno.EEXIST:
                raise FileExistsError(first_error, os.strerror(first_error))
            result = linkat(
                -100,
                os.fsencode("/proc/self/fd/{}".format(descriptor)),
                directory_descriptor,
                destination_bytes,
                0x400,
            )
            if result != 0:
                fallback_error = ctypes.get_errno()
                if fallback_error == errno.EEXIST:
                    raise FileExistsError(fallback_error, os.strerror(fallback_error))
                raise OSError(
                    fallback_error,
                    "linkat fd 发布失败: {} / {}".format(
                        os.strerror(first_error), os.strerror(fallback_error)
                    ),
                )
    elif sys.platform == "darwin":
        try:
            fclonefileat = library.fclonefileat
        except AttributeError as exc:
            raise OSError(errno.ENOTSUP, "fclonefileat 不可用") from exc
        fclonefileat.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        fclonefileat.restype = ctypes.c_int
        if fclonefileat(descriptor, directory_descriptor, destination_bytes, 0) != 0:
            clone_error = ctypes.get_errno()
            if clone_error == errno.EEXIST:
                raise FileExistsError(clone_error, os.strerror(clone_error))
            raise OSError(clone_error, os.strerror(clone_error))
    else:
        raise OSError(errno.ENOTSUP, "当前平台没有 fd-safe no-replace 发布原语")
except FileExistsError:
    os.close(directory_descriptor)
    raise SystemExit(
        "拒绝覆盖竞争进程创建的 image tar；staging 保留: {} "
        "(device={}, inode={})".format(
            staging, expected_identity[0], expected_identity[1]
        )
    ) from None
except Exception as exc:
    os.close(directory_descriptor)
    raise SystemExit(
        "{}；未自动清理 staging: {} (device={}, inode={})".format(
            exc, staging, expected_identity[0], expected_identity[1]
        )
    ) from None
finally:
    os.close(descriptor)

published_descriptor = os.open(
    destination.name, flags, dir_fd=directory_descriptor
)
try:
    published_stat = os.fstat(published_descriptor)
    published_identity = (published_stat.st_dev, published_stat.st_ino)
    if not stat.S_ISREG(published_stat.st_mode):
        raise SystemExit("发布后的 image tar 不是普通文件")
    if sha256_fd(
        published_descriptor, published_identity, "发布后的 image tar"
    ) != staging_digest:
        raise SystemExit("发布后的 image tar 摘要不一致")
    os.fsync(published_descriptor)
finally:
    os.close(published_descriptor)

current_parent_stat = os.lstat(destination.parent)
if (
    not stat.S_ISDIR(current_parent_stat.st_mode)
    or (current_parent_stat.st_dev, current_parent_stat.st_ino)
    != expected_parent_identity
):
    raise SystemExit("image tar 发布后父目录 identity 已变化")

checksum_payload = "{}  {}\n".format(
    staging_digest, destination.name
).encode("ascii")
checksum_flags = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
try:
    checksum_descriptor = os.open(
        destination.name + ".sha256",
        checksum_flags,
        0o600,
        dir_fd=directory_descriptor,
    )
except OSError as exc:
    raise SystemExit(
        "tar 已发布但 checksum 未安全创建；staging 与 tar 均保留"
    ) from None
try:
    os.fchmod(checksum_descriptor, 0o600)
    view = memoryview(checksum_payload)
    while view:
        written = os.write(checksum_descriptor, view)
        view = view[written:]
    os.fsync(checksum_descriptor)
finally:
    os.close(checksum_descriptor)
try:
    os.fsync(directory_descriptor)
finally:
    os.close(directory_descriptor)
print(
    staging,
    expected_identity[0],
    expected_identity[1],
    staging_digest,
    sep="\t",
)
PY_IMAGE_ARCHIVE
)"

IFS=$'\t' read -r IMAGE_TAR_STAGE IMAGE_TAR_STAGE_DEV IMAGE_TAR_STAGE_INO \
  IMAGE_TAR_SHA256 <<< "$IMAGE_ARCHIVE_REPORT"
[[ "$IMAGE_TAR_SHA256" =~ ^[0-9a-f]{64}$ ]]
printf 'staging_path\tdevice\tinode\tsha256\n%s\t%s\t%s\t%s\n' \
  "$IMAGE_TAR_STAGE" "$IMAGE_TAR_STAGE_DEV" "$IMAGE_TAR_STAGE_INO" \
  "$IMAGE_TAR_SHA256" \
  > "$EVIDENCE_ROOT/old-images-tar-staging.tsv"
chmod 0600 "$EVIDENCE_ROOT/old-images-tar-staging.tsv"
```

把 `stack-images.tsv`、tar、`old-images.tar.sha256` 和 staging 证据一起复制到只读介质；
隐藏 tar staging 不自动清理。Linux 通常与正式 tar 同 inode，macOS clone 为不同 inode；
两者均已由 fd 重新计算摘要并校验一致。另一台 Docker 主机使用 5.2 的 held-fd load
wrapper，再直接用三个 recorded ID 执行 `docker image inspect`，完成可加载性演练。

### 5.2 回滚顺序

1. 宣布维护窗口，停止 `celery-acq`、`celery-short`、`django`、`web`、`redis`，
   保留原数据。
2. 若新版本做过不向后兼容的数据迁移，先把环境变量切到第 3、4 节验证通过的
   SQLite 新文件和 Influx 新 bucket；不要覆盖或删除新旧数据。
3. 生成 `0600` Compose override，把六个服务的 `image` 直接绑定到已校验 immutable ID，
   使用 `--pull never` 重建。不创建或依赖任何可变 image tag；rollback 期间
   所有后续重启都必须保留该 override：

```bash
docker compose --env-file "$COMPOSE_ENV_FILE" \
  -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" --profile ui \
  stop celery-acq celery-short django web redis

python3 - "$EVIDENCE_ROOT/old-images.tar" \
  "$EVIDENCE_ROOT/old-images.tar.sha256" 3600 <<'PY_IMAGE_LOAD'
import hashlib
import math
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

archive = Path(os.path.abspath(sys.argv[1]))
checksum = Path(os.path.abspath(sys.argv[2]))
timeout = float(sys.argv[3])
if archive.parent != checksum.parent:
    raise SystemExit("image tar 与 checksum 必须在同一安全父目录")
if not math.isfinite(timeout) or timeout <= 0:
    raise SystemExit("image load timeout 必须是正有限值")
parent_stat = os.lstat(archive.parent)
if (
    not stat.S_ISDIR(parent_stat.st_mode)
    or stat.S_ISLNK(parent_stat.st_mode)
    or parent_stat.st_uid != os.geteuid()
    or stat.S_IMODE(parent_stat.st_mode) & 0o022
):
    raise SystemExit("image tar 父目录不是当前 UID 控制的安全目录")
parent_identity = (parent_stat.st_dev, parent_stat.st_ino)
directory_flags = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
regular_flags = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
directory_descriptor = os.open(archive.parent, directory_flags)
archive_descriptor = None
checksum_descriptor = None

def stable_pread(fd, expected_identity, maximum=None):
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or (
        before.st_dev, before.st_ino
    ) != expected_identity:
        raise RuntimeError("image evidence fd identity 不一致")
    if maximum is not None and before.st_size > maximum:
        raise RuntimeError("image checksum 文件过大")
    output = bytearray()
    offset = 0
    while offset < before.st_size:
        chunk = os.pread(fd, min(1024 * 1024, before.st_size - offset), offset)
        if not chunk:
            raise RuntimeError("image evidence 读取提前结束")
        output.extend(chunk)
        offset += len(chunk)
    after = os.fstat(fd)
    if (
        (after.st_dev, after.st_ino) != expected_identity
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise RuntimeError("image evidence 读取期间变化")
    return bytes(output)

def sha256_fd(fd, expected_identity):
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or (
        before.st_dev, before.st_ino
    ) != expected_identity:
        raise RuntimeError("image tar fd identity 不一致")
    digest = hashlib.sha256()
    offset = 0
    while offset < before.st_size:
        chunk = os.pread(fd, min(1024 * 1024, before.st_size - offset), offset)
        if not chunk:
            raise RuntimeError("image tar hash 提前结束")
        digest.update(chunk)
        offset += len(chunk)
    after = os.fstat(fd)
    if (
        (after.st_dev, after.st_ino) != expected_identity
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise RuntimeError("image tar hash 期间变化")
    return digest.hexdigest()

try:
    opened_parent_stat = os.fstat(directory_descriptor)
    if (opened_parent_stat.st_dev, opened_parent_stat.st_ino) != parent_identity:
        raise RuntimeError("image evidence 父目录 identity 已变化")
    checksum_descriptor = os.open(
        checksum.name, regular_flags, dir_fd=directory_descriptor
    )
    checksum_stat = os.fstat(checksum_descriptor)
    checksum_identity = (checksum_stat.st_dev, checksum_stat.st_ino)
    checksum_bytes = stable_pread(checksum_descriptor, checksum_identity, 1024)
    match = re.fullmatch(
        rb"([0-9a-f]{64})  old-images\.tar\n", checksum_bytes
    )
    if match is None:
        raise RuntimeError("image checksum 格式非法")
    expected_digest = match.group(1).decode("ascii")

    archive_descriptor = os.open(
        archive.name, regular_flags, dir_fd=directory_descriptor
    )
    archive_stat = os.fstat(archive_descriptor)
    archive_identity = (archive_stat.st_dev, archive_stat.st_ino)
    if archive_stat.st_size <= 0:
        raise RuntimeError("image tar 为空")
    actual_digest = sha256_fd(archive_descriptor, archive_identity)
    if actual_digest != expected_digest:
        raise RuntimeError("image tar checksum 不一致")

    os.lseek(archive_descriptor, 0, os.SEEK_SET)
    try:
        completed = subprocess.run(
            ["docker", "image", "load"],
            stdin=archive_descriptor,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("docker image load 超时；stdout/stderr 已抑制") from None
    except OSError:
        raise RuntimeError("无法执行 docker image load；stdout/stderr 已抑制") from None
    if completed.returncode != 0:
        raise RuntimeError("docker image load 失败；stdout/stderr 已抑制")
    if sha256_fd(archive_descriptor, archive_identity) != expected_digest:
        raise RuntimeError("image tar 在 load 期间变化")
    current_archive_stat = os.stat(
        archive.name, dir_fd=directory_descriptor, follow_symlinks=False
    )
    current_checksum_stat = os.stat(
        checksum.name, dir_fd=directory_descriptor, follow_symlinks=False
    )
    if (
        (current_archive_stat.st_dev, current_archive_stat.st_ino) != archive_identity
        or (current_checksum_stat.st_dev, current_checksum_stat.st_ino)
        != checksum_identity
    ):
        raise RuntimeError("image evidence pathname 在 load 期间变化")
    current_parent_stat = os.lstat(archive.parent)
    if (
        not stat.S_ISDIR(current_parent_stat.st_mode)
        or (current_parent_stat.st_dev, current_parent_stat.st_ino)
        != parent_identity
    ):
        raise RuntimeError("image evidence 父目录 pathname 在 load 期间变化")
except Exception as exc:
    raise SystemExit(str(exc)) from None
finally:
    if archive_descriptor is not None:
        os.close(archive_descriptor)
    if checksum_descriptor is not None:
        os.close(checksum_descriptor)
    os.close(directory_descriptor)
PY_IMAGE_LOAD

image_id_for_service() {
  awk -F '\t' -v wanted="$1" '$1 == wanted { print $3 }' \
    "$EVIDENCE_ROOT/stack-images.tsv"
}
BACKEND_IMAGE_ID="$(image_id_for_service celery-acq)"
WEB_IMAGE_ID="$(image_id_for_service web)"
REDIS_IMAGE_ID="$(image_id_for_service redis)"
[[ "$BACKEND_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$WEB_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]]
[[ "$REDIS_IMAGE_ID" =~ ^sha256:[0-9a-f]{64}$ ]]
test "$(docker image inspect "$BACKEND_IMAGE_ID" --format '{{.Id}}')" = "$BACKEND_IMAGE_ID"
test "$(docker image inspect "$WEB_IMAGE_ID" --format '{{.Id}}')" = "$WEB_IMAGE_ID"
test "$(docker image inspect "$REDIS_IMAGE_ID" --format '{{.Id}}')" = "$REDIS_IMAGE_ID"

export ROLLBACK_OVERRIDE_FILE="$EVIDENCE_ROOT/rollback-images-${RECOVERY_ID}.override.yml"
python3 - "$ROLLBACK_OVERRIDE_FILE" "$BACKEND_IMAGE_ID" \
  "$WEB_IMAGE_ID" "$REDIS_IMAGE_ID" <<'PY_ROLLBACK_OVERRIDE'
import os
import re
import stat
import sys
from pathlib import Path

path = Path(os.path.abspath(sys.argv[1]))
backend_id, web_id, redis_id = sys.argv[2:]
for image_id in (backend_id, web_id, redis_id):
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
        raise SystemExit("rollback override 收到非法 image ID")
parent_stat = os.lstat(path.parent)
if (
    not stat.S_ISDIR(parent_stat.st_mode)
    or stat.S_ISLNK(parent_stat.st_mode)
    or parent_stat.st_uid != os.geteuid()
    or stat.S_IMODE(parent_stat.st_mode) & 0o022
):
    raise SystemExit("rollback override 父目录不安全")
payload = """services:
  redis:
    image: {redis}
  migrate:
    image: {backend}
  celery-acq:
    image: {backend}
  celery-short:
    image: {backend}
  django:
    image: {backend}
  web:
    image: {web}
""".format(redis=redis_id, backend=backend_id, web=web_id).encode("ascii")
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(path, flags, 0o600)
try:
    os.fchmod(descriptor, 0o600)
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        view = view[written:]
    os.fsync(descriptor)
finally:
    os.close(descriptor)
directory_descriptor = os.open(
    path.parent,
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
)
try:
    os.fsync(directory_descriptor)
finally:
    os.close(directory_descriptor)
PY_ROLLBACK_OVERRIDE
test ! -L "$ROLLBACK_OVERRIDE_FILE"
if stat --version >/dev/null 2>&1; then
  ROLLBACK_OVERRIDE_MODE="$(stat -c '%a' -- "$ROLLBACK_OVERRIDE_FILE")"
else
  ROLLBACK_OVERRIDE_MODE="$(stat -f '%Lp' "$ROLLBACK_OVERRIDE_FILE")"
fi
test "$ROLLBACK_OVERRIDE_MODE" = '600'

docker compose --env-file "$COMPOSE_ENV_FILE" \
  -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" -f "$ROLLBACK_OVERRIDE_FILE" \
  --profile ui up -d --force-recreate --pull never \
  redis migrate celery-acq celery-short django web

for ROLLBACK_SERVICE in redis migrate celery-acq celery-short django web; do
  ROLLBACK_CONTAINER_ID="$(docker compose --env-file "$COMPOSE_ENV_FILE" \
    -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" -f "$ROLLBACK_OVERRIDE_FILE" \
    ps -aq "$ROLLBACK_SERVICE")"
  test -n "$ROLLBACK_CONTAINER_ID"
  test "$(printf '%s\n' "$ROLLBACK_CONTAINER_ID" | wc -l | tr -d ' ')" = '1'
  case "$ROLLBACK_SERVICE" in
    redis) EXPECTED_ROLLBACK_IMAGE_ID="$REDIS_IMAGE_ID" ;;
    web) EXPECTED_ROLLBACK_IMAGE_ID="$WEB_IMAGE_ID" ;;
    *) EXPECTED_ROLLBACK_IMAGE_ID="$BACKEND_IMAGE_ID" ;;
  esac
  test "$(docker inspect "$ROLLBACK_CONTAINER_ID" --format '{{.Image}}')" \
    = "$EXPECTED_ROLLBACK_IMAGE_ID"
done
docker compose --env-file "$COMPOSE_ENV_FILE" \
  -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" -f "$ROLLBACK_OVERRIDE_FILE" ps \
  | tee "$EVIDENCE_ROOT/compose-after-rollback.txt"
```

5. 验证首页/API、配置关键表计数、采集会话、Influx 最新时间、WebSocket 实时值和
   最近告警。任何一项不通过都保持维护状态并升级处理，不执行清库重试。

## 6. M0 恢复演练验收清单

- [ ] SQLite 在线备份报告：integrity ok、关键表无缺失、计数已人工复核；
- [ ] SQLite 新文件恢复报告：两次 integrity ok、计数完全相同；
- [ ] Influx archive：显式 host/org/bucket/path、writer 原状态恢复、前后结构/范围摘要
      稳定、SHA-256 通过；
- [ ] Influx 新 bucket：restore 前不存在，恢复后结构/数量/时间范围摘要一致，并完成
      关键业务值抽样；
- [ ] 旧镜像：inspect/image ID 留档、tar checksum 通过、独立主机 load 成功；
- [ ] 回滚启动：API/页面/采集/时序查询/实时推送/告警全部通过；
- [ ] 原 SQLite、原 Influx bucket、旧镜像归档均仍保留，且没有执行 destructive reset。

未完成以上全部项目时，M0 只能标记为“安全网实现完成、生产恢复尚未演练”，不能标记
为生产恢复已验收。

截至本手册进入 M0 分支时，Influx wrapper 只完成了隔离单元测试（伪 CLI 的参数、
超时、空 bucket、checksum、已有 bucket 拒绝和结构/范围比对）；**尚未对真实 InfluxDB
OSS 2.x 执行 backup/restore 演练**。因此上面两项 Influx checkbox 必须保持未勾选，
直到在授权的独立实例或维护窗口取得真实证据。

## 7. 已知边界

- SQLite 的不覆盖发布在 Linux 依赖 `linkat(AT_EMPTY_PATH)` 或只引用已验证 fd 的
  `/proc/self/fd` fallback，在 macOS 依赖 `fclonefileat`；文件系统/平台不支持时安全失败，
  不会退化成按临时路径 hard-link 或覆盖式 rename。Linux 结果通常与 staging 同 inode；
  macOS clone 是新 inode，只承诺完整摘要一致，并可能额外占用空间。
- SQLite 随机临时文件、Influx staging 和 image tar staging 永不自动删除。成功 Influx
  backup staging 是空目录；Influx restore staging 保留 `0500/0400` 的已验证只读副本；
  Linux 的 image tar staging 与正式 tar 通常同 inode，macOS clone 只保证摘要一致且会
  使用不同 inode。失败项可能保留部分诊断数据。只能在隔离父
  目录并按报告/证据中的精确路径现场核对 identity 与摘要后逐个清理。若父目录被移动或
  临时路径被替换，原路径可能无法定位保留 inode，禁止通配符或仅按路径直接清理。
- Python 标准库不能让 `sqlite3.Connection` 直接接管既有 fd；本工具只面向单线程 CLI，
  采用“连接后、首条 SQL/PRAGMA 前唯一新增 fd 与 mkstemp inode 对账”的最小安全方案。
  SQLite VFS 对源 DB/WAL/SHM 仍按路径工作；若威胁模型包含同 UID 恶意进程持续替换源
  路径，需要原生 fd-aware VFS 或在隔离维护窗口执行，不能把本工具描述为形式化消除
  所有 VFS 路径竞态。
- Influx 目录原子不覆盖发布依赖 macOS `renamex_np(RENAME_EXCL)` 或 Linux
  `renameat2(RENAME_NOREPLACE)`；平台/文件系统不支持时安全失败并保留 staging。
- SQLite 在线备份容器以宿主操作者 uid/gid 运行；源 DB 及存在的 WAL/SHM 对该身份不可读
  时预检失败。恢复写 `/data` 使用镜像默认 root，不能把该差异误当作放宽备份权限。
- 六服务基线中任一容器缺失或不唯一都会停止预检；只有现场负责人留下 `0600`、非 Git
  的显式批准证据并重新建立可逐项 env/command 对账的容器基线后才可继续。
- EXIT/信号 trap 无法处理 `SIGKILL`、宿主机掉电或 Docker daemon 崩溃；此类情况
  必须依据 before 状态人工核对并恢复 `celery-acq`。
- Influx restore 超时或服务端失败可能留下新 bucket 或服务端临时恢复状态。工具不会
  自动删除它；隔离该名字、保留日志并按 Influx 官方失败恢复流程人工处理。
- count/first/last 摘要不会发现相同时间戳与数量下的值替换，不能代替关键点逐值抽样、
  业务查询和真实恢复演练。
