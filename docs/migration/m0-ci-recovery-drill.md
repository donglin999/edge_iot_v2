# M0 隔离恢复/回滚 CI 演练附录

更新时间：2026-08-19（Asia/Shanghai）

本附录把 `m0-recovery-runbook.md` 的关键恢复能力变成可重复 CI 门禁。它只验证全新、
一次性的测试事实源，不读取或修改演示/生产容器、bucket、volume、端口或凭据。现场恢复仍以
主手册的人工预检、维护窗口和审批为准；本演练不能代替现场备份。

## 隔离边界

- Compose project 必须匹配 `m0ci-…` 且在启动前不存在任何同 project 容器、卷或网络。
- 真正执行前会对唯一的非测试 Shell 入口 `m0_ci_drill.sh` 原始字节做已审 SHA-256 冻结校验；
  这是“源码发生任何变化就必须重新审查并更新摘要”的完整性回归门禁，不宣称能推导任意 Shell 语义。
- Compose 文件不允许 `container_name`、`network_mode: host` 或 external resource；应用网络是
  `internal: true` 的 bridge。DinD 另建仅属于本 project 的 internal bridge，并在结束时按
  Docker 返回的 network ID 精确删除。DinD 不发布任何宿主端口，只监听容器内 Unix socket；
  held-fd 镜像流通过宿主 `docker exec -i <immutable-container-id> docker image load` 送入该 socket。
  container ID 必须是 `docker run` 返回的完整 64 位 immutable ID，不接受可变容器名，也不落盘
  客户端证书或暴露远程 Docker API。启动后还会核对实际 image/command/label/空 port binding/唯一
  network；退出时使用精确 container ID 和 `rm -v` 删除 `/var/lib/docker` 匿名卷并验证它已不存在。
- Web 与 Influx 的随机宿主端口只绑定 `127.0.0.1`。Redis、Django、Celery 不发布端口。
- SQLite、Influx data/config 都使用 project 私有新卷。退出 trap 只删除这个已验证 project
  的资源，从不调用 prune，也不按模糊名称删除资源。
- 所有镜像必须先显式 prepare；演练收到的引用只能是六个 `sha256:<64 hex>` immutable ID。
  Compose 每个服务均设置 `pull_policy: never`，演练阶段不 build、不 pull。
- evidence 必须是指定私有根目录的全新直接子目录，名称包含 project，创建后权限精确为
  `0700`。目录通过原子 `os.mkdir` 创建；演练进程一直持有并复查 dirfd/device/inode。所有文本
  证据通过 `O_EXCL|O_NOFOLLOW` 独占创建，不使用 shell 截断重定向。随机 Influx token 以
  `0600` 独立放在 allowed-root，不位于 evidence 可写树内，trap 只在 device/inode 一致时删除。
  Django secret 与 Influx 初始化密码同样逐次随机生成。
- 所有服务都有 CPU、memory、pids 上限；DinD 额外使用 `768m/1.5 CPU/512 pids` 上限。

## 实际验证链

1. 在全新 app-db 卷执行 Django migration 并写入已知 Site、可由 REST/WS 读取的暂停采集
   Session、DataPoint 和 firing Alarm。使用暂停态可防止较慢的镜像归档阶段被误判为孤儿任务。
2. 使用现有 `scripts/backup/sqlite_backup.py` 对 WAL 安全在线备份，再恢复到全新
   `/data/restored.sqlite3`；回滚服务只读取恢复文件。
3. 在全新 InfluxDB OSS 2.x 写入纳秒时间戳的 `temperature=42.5`，使用现有 wrapper 做
   real backup、checksum verify；restore 前用 `--limit 0` 成功枚举 org 全部 bucket 并在本地
   精确确认 `m0-restored` 不存在。exit 0 但空 stdout 仍拒绝，只有显式 JSON `[]` 才表示空集；
   随后恢复到该新 bucket 并查询精确已知值。
4. 将 backend、web、Redis 三个 immutable ID 通过 held-fd 写入新 tar 并生成 SHA-256
   scope 文档；Linux 使用 `linkat(fd)`、Darwin 使用 `fclonefileat(fd)`，不解析可变 staging
   pathname。load 时从同一个 held fd 分块读取，每块在写入 Docker stdin 的同时更新 SHA-256；
   最终流摘要必须与 scope 文档一致，避免“加载攻击字节后再把源文件恢复”的前后哈希绕过。
   该流通过 internal network 上无宿主端口的 `docker exec -i` 送入独立、空白 DinD daemon，
   并逐个 inspect 原 ID。
5. 用恢复后的 SQLite、恢复后的 Influx bucket 以及 SHA-pinned backend/web/Redis 启动回滚栈。
   对每个运行容器重新核对顶层 `.Image`。
6. 经 Nginx 真实执行 SPA HTTP、Site/Session/DataPoint/Alarm API、RFC 6455 WebSocket upgrade
   与恢复后初始首帧读取。这只证明**恢复后的静态事实源可读**，不声称验证实时采集、动态告警
   或消息推送链路。
7. 分别定向完整唯一节点名检查 acquisition 与 short 两个 Celery worker 的 ping，并检查
   `active_queues` 确实为 `acquisition` / `short`，不以单个模糊 `pong` 代替双 worker 验收。

成功证据在 runner-local `summary.json` 汇总；SQLite/Influx wrapper 报告、archive checksum、
DinD load 报告、静态事实源 smoke 报告以及大体积镜像 tar 都只存在于本次 runner 的私有
evidence 目录。workflow 不调用 artifact uploader，因而 token、数据库、tar、
hardlink/别名或报告元数据都没有 GitHub Actions artifact 上传边界；CI 日志只输出门禁成功/失败
状态。Influx restore wrapper 按安全设计保留只读 restore staging，随 project/evidence 生命周期
结束，不会触碰源 bucket。

## 本地运行

普通本地开发不应直接运行完整演练。先在隔离 Docker daemon 上显式构建/拉取 workflow 所列
镜像，解析六个 immutable ID 并导出 `M0_*_IMAGE_ID`，再设置
`M0_IMAGES_PREPARED=1`。调用示例：

```bash
bash scripts/recovery/m0_ci_drill.sh \
  --project m0ci-local-20260818 \
  --allowed-root /private/tmp \
  --evidence /private/tmp/evidence-m0ci-local-20260818
```

若 project 或 evidence 已存在、镜像不是 ID、目标资源无法唯一证明为空、Compose 出现外部
资源、恢复目标已存在、数据摘要或已知值不一致，脚本都会 fail closed，不启动或不切换回滚栈。

快速、非 Docker 门禁：

```bash
bash scripts/recovery/tests/test_static.sh
```

该门禁只依赖 Bash、Python 和基础 `grep`；危险源扫描由 Python 实现，即使 PATH 中不存在
`rg` 也不会静默跳过。
