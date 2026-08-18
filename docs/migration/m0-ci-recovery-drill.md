# M0 隔离恢复/回滚 CI 演练附录

更新时间：2026-08-18（Asia/Shanghai）

本附录把 `m0-recovery-runbook.md` 的关键恢复能力变成可重复 CI 门禁。它只验证全新、
一次性的测试事实源，不读取或修改演示/生产容器、bucket、volume、端口或凭据。现场恢复仍以
主手册的人工预检、维护窗口和审批为准；本演练不能代替现场备份。

## 隔离边界

- Compose project 必须匹配 `m0ci-…` 且在启动前不存在任何同 project 容器、卷或网络。
- Compose 文件不允许 `container_name`、`network_mode: host` 或 external resource；唯一网络是
  `internal: true` 的 bridge。
- Web 与 Influx 的随机宿主端口只绑定 `127.0.0.1`。Redis、Django、Celery 不发布端口。
- SQLite、Influx data/config 都使用 project 私有新卷。退出 trap 只删除这个已验证 project
  的资源，从不调用 prune，也不按模糊名称删除资源。
- 所有镜像必须先显式 prepare；演练收到的引用只能是六个 `sha256:<64 hex>` immutable ID。
  Compose 每个服务均设置 `pull_policy: never`，演练阶段不 build、不 pull。
- evidence 必须是指定私有根目录的全新直接子目录，名称包含 project，创建后权限精确为
  `0700`。随机 Influx token 文件为 `0600`，不上传为 CI artifact。

## 实际验证链

1. 在全新 app-db 卷执行 Django migration 并写入已知 Site、可由 REST/WS 读取的暂停采集
   Session、DataPoint 和 firing Alarm。使用暂停态可防止较慢的镜像归档阶段被误判为孤儿任务。
2. 使用现有 `scripts/backup/sqlite_backup.py` 对 WAL 安全在线备份，再恢复到全新
   `/data/restored.sqlite3`；回滚服务只读取恢复文件。
3. 在全新 InfluxDB OSS 2.x 写入纳秒时间戳的 `temperature=42.5`，使用现有 wrapper 做
   real backup、checksum verify，并 restore 到事前不存在的 `m0-restored` bucket；再次查询
   精确已知值。
4. 将 backend、web、Redis 三个 immutable ID 通过 held-fd 写入新 tar 并生成 SHA-256
   scope 文档；held-fd 复验后将其 load 到独立、空白 DinD daemon，并逐个 inspect 原 ID。
5. 用恢复后的 SQLite、恢复后的 Influx bucket 以及 SHA-pinned backend/web/Redis 启动回滚栈。
   对每个运行容器重新核对顶层 `.Image`。
6. 经 Nginx 真实执行 SPA HTTP、Site/Session/DataPoint/Alarm API、RFC 6455 WebSocket upgrade
   与首帧内容检查；同时检查 Celery worker ping。

成功证据在 `summary.json` 汇总；CI artifact 还保留 SQLite/Influx wrapper 报告、archive
checksum 文档、DinD load 报告和全栈 smoke 报告。大体积镜像 tar 与一次性 token 明确排除
上传。Influx restore wrapper 按安全设计保留只读 restore staging，随 project/evidence 生命周期
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
