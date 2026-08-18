# Vue 3 + FastAPI 渐进迁移里程碑与发布门禁

## 1. 目标与范围

本计划把现有 React + Django/DRF/Channels 控制面渐进迁移到 Vue 3 +
FastAPI，同时保留现场已经验证过的 SQLite、Redis、Celery、InfluxDB 和工业协议
采集链路。迁移采用绞杀者模式，每个里程碑都必须能够独立验收和回滚。

以下约束贯穿 M0～M9：

- 对外 `/api/`、`/ws/` 路径、分页格式、错误格式和 InfluxDB schema 默认保持兼容。
- SQLite 与 InfluxDB 不在本次框架迁移中换库，避免同时叠加数据迁移风险。
- **同一条配置写接口在任一时刻只能有一个生产写入者**，禁止 Django 与
  FastAPI 双写同一业务数据。
- **同一设备、采集任务或 MQTT/SCADA 账号在任一时刻只能有一个采集器**。
  禁止新旧 worker 双跑；SCADA 单会话账号双跑会互踢并产生 142 错误。
- API 或前端灰度可以并行，采集器切换必须采用“旧实例退出并 flush，再启动新实例”
  的有界停顿交接。
- 共存阶段数据库变更只允许 expand（新增表、列或索引），不删除、不重命名旧字段。
- 生产机只负责 `docker load` 和启动，禁止在现场 `docker build`、`pip install` 或
  `npm install`。
- C++ 后端不属于本路线。新分支不纳入 `backend_cpp/`、C++ 构建服务或 C++ 迁移
  文档；离线镜像的 `.dockerignore` 继续显式排除本机遗留目录，防止误打包。

## 2. Git 与远端规则

### 2.1 工作区隔离

迁移工作必须在独立 worktree 中完成，不得清理、重置或全量 stash 原工作区：

```bash
git fetch --prune origin
git fetch --prune midea
git worktree add ../edge_iot_v2-remediation \
  -b codex/remediation-integration origin/feature/zhongshan-scada
```

创建前必须记录基线 SHA，并确认选定基线在两个远端的状态。不得默认从 `master`
开始，因为当前业务发布线可能包含尚未进入 `master` 的现场功能。

### 2.2 分支、提交和推送

- `codex/remediation-integration` 是稳定集成分支，不直接在该分支开发。
- 每个里程碑使用短分支：`codex/remediation-mX-<scope>`。
- 每个提交只包含一个可解释的逻辑变化；迁移代码、兼容测试和必要文档应在同一
  里程碑 PR 中闭环。
- 提交信息使用 `chore(m0): ...`、`feat(m3): ...`、`test(m8): ...`、
  `release(m9): ...` 等格式。
- 只显式暂存目标文件，禁止 `git add .`；提交前必须执行：

```bash
git diff --cached --check
git diff --cached --stat
git diff --cached
```

- `origin` 是 PR 与 CI 的主远端。先推 `origin`，CI 和评审通过并得到 accepted SHA
  后，再把**同一个 SHA**同步到 `midea`，不得在两个远端分别合并：

```bash
git push -u origin codex/remediation-mX-scope
# CI、评审和合并通过后
git push midea <accepted-sha>:refs/heads/codex/remediation-mX-scope
```

- 每次同步后用 `git ls-remote` 比较两个远端的 branch/tag SHA。
- 禁止直接推送受保护的发布分支，禁止用普通 `--force`；确需更新评审分支时只能
  使用 `--force-with-lease`。
- GitHub CLI 认证是 M0 门禁。`gh auth status` 失败时不得声称 PR、checks 或发布
  已完成，应先执行 `gh auth login -h github.com -p https -w` 并再次验证。

### 2.3 多 Agent 协作与集成

- 每个并行 Agent 只领取一个边界清晰的领域（例如 REST 契约、实时/Excel 契约、
  Vue 页面或 FastAPI 路由），并在任务开始时声明文件 owner。
- 写代码的 Agent 使用独立 worktree 和短分支；不得让两个 Agent 同时修改同一文件。
  共享文件（依赖锁、路由注册、Compose、部署脚本）只由集成人员修改。
- Agent 交接必须包含：变更文件、关键决策、已执行命令、精确测试结果、已知风险和
  尚未完成项。不得仅以“代码已写完”作为完成依据。
- 功能作者不得自行给自己的里程碑放行。独立 reviewer 检查 diff、兼容性与回滚，
  集成人员汇总所有测试证据后才允许 commit/push。
- 集成顺序固定为“领域分支测试 → 独立审查 → 显式暂存 → 本地全量门禁 → commit
  → push origin → CI → PR 合入集成分支”；发生冲突后必须重新执行全量门禁。

### 2.4 禁止纳入提交的本地数据

除非里程碑明确审批，下列内容不得进入迁移提交：

- `db.sqlite3-wal`、`db.sqlite3-shm` 和任何生产/调试数据库；
- `work/`、InfluxDB 备份、日志、截图和临时导出；
- 本机端口专用的 `docker-compose.override.yml`；
- token、密码、`.env` 和其它密钥材料。

## 3. 所有里程碑的公共门禁

每个 Mx 在本地 commit 和 push 前均需满足以下门禁。push 后还必须通过远端 CI 与
独立评审，才能合入集成分支；任一必需测试未通过时不得以“先推再修”为由推送：

1. worktree 在测试前后没有与该里程碑无关的改动。
2. 后端全量测试和 e2e 子集通过：

   ```bash
   cd backend
   python -m pytest tests/ -c tests/pytest.ini
   python -m pytest tests/ -c tests/pytest.ini -m e2e
   ```

3. 前端依赖锁定、lint、单测和生产构建通过：

   ```bash
   cd frontend
   npm ci
   npm run lint
   npm run test
   npm run build
   ```

4. Compose 配置可以完整解析：

   ```bash
   docker compose config
   docker compose -f deploy/offline/docker-compose.offline.yml config
   ```

5. 涉及 API、WS、数据或部署的变更必须在一次性测试环境运行真实服务并保存验收
   证据；会写数据的浏览器测试不得指向生产库。
6. PR 描述列明：范围、兼容性影响、测试结果、数据迁移、单写入者归属、回滚步骤和
   accepted SHA。
7. 在 PR 合入后对 accepted SHA 再执行或核对同等 CI；回滚未验证、CI 未通过或两个
   远端 SHA 不可核对时，不得进入下一里程碑。

## 4. M0～M9

### M0：隔离、决策和可回滚基线

目标：建立干净 worktree、唯一迁移路线和可重复的现状基线。

现状、证据和未通过项统一记录在 [`m0-baseline.md`](./m0-baseline.md)，不得只在聊天
或本机日志中口头宣称完成。

交付物：

- 固定源分支与基线 SHA，确认 FastAPI 是唯一新后端方向；
- 记录现有服务拓扑、API/WS 路径、数据库表、Influx measurement/tag/field；
- 保存代表性配置导出、API 响应和 WebSocket 消息样本；
- 记录当前测试耗时、API p95、采集速率、丢弃数和容器资源占用；
- 确认 `origin`、`midea` 读写权限以及 GitHub CLI 认证；
- 明确 SQLite、InfluxDB 和旧镜像的备份/恢复命令。

门禁：现有 CI 全绿，当前 Docker 栈和 Mock 全流程可复现，备份恢复至少在测试环境
演练一次。M0 只建立基线，不切流量、不修改生产数据。

说明：里程碑计划文档、worktree 和测试环境的初始化提交只算 M0 准备工作。为了缩短
总工期，下一里程碑可以在独立、不切流量的 worktree 中并行实现；但只有上列交付物与
门禁证据全部归档后，才能把 M0 标记为 accepted、合入下一里程碑或改变运行行为。

### M1：API、WebSocket 与数据契约安全网

目标：在改框架前把前端真正依赖的行为固化成黑盒契约。

交付物：

- REST 路径、HTTP 方法、尾斜杠、状态码、分页和错误响应契约；
- Decimal、时区、空值、multipart、Excel 下载文件名和响应头契约；
- 两条 WebSocket 路径、消息类型、重连及去重契约；
- Influx measurement、tags、fields、纳秒时间戳和查询窗口契约；
- 可同时对 Django 与后续 FastAPI 执行的 contract/golden 测试。

门禁：契约测试对当前 Django 全绿，fixture 不含现场密钥或生产数据。只提交测试、
fixture 与文档，不改变运行行为。

### M2：Vue 3 平行壳与共享 API 层

目标：建立 Vue 3 + TypeScript + Vite 应用骨架，继续只调用 Django API。

交付物：

- Vue Router、布局、主题、错误边界和 WebSocket composable；
- 复用或迁移 Axios 请求类型，保持相对 `/api`、`/ws` 地址；
- 独立端口或 `/next/` 入口，旧 React 首页保持不变；
- 首批只读页面和组件测试。

门禁：Vue 与 React 可同时访问，但所有写请求仍只有 Django API 一个写入者；Vue
构建产物不得替换当前生产入口。新旧前端读取同一 API 的关键展示结果一致。

### M3：Vue 3 普通业务页面

目标：先迁移低风险、以 REST CRUD 为主的页面，继续调用既有 Django API。

交付物：

- 仪表盘、设备列表/详情、配置导入、版本历史和告警页面；
- 通用协议字段表单、连接测试、设备编辑和 Excel 导入组件；
- loading、空态、分页、错误态和取消请求等价测试；
- 每个垂直页面切片的 Vue 单测与真 Django API 冒烟。

门禁：上述 6 个业务路由与 React 行为等价；设备 CRUD、连接测试、通用协议 Excel、
告警确认/规则 CRUD、版本查看/回滚在一次性测试数据中通过。SCADA 专属配置、采集控制
和数据可视化仍留在 M4，M3 不切默认入口。

### M4：Vue 3 高风险实时功能

目标：迁移 SCADA、采集控制和数据可视化三条高风险关键路径。

交付物：

- SCADA 网关及 N 设备 × M 测点动态表单、Excel round-trip；
- 采集任务启停、WebSocket 断线重连、1Hz 消息聚合和会话日志；
- 历史图、离散/文本值展示与大测点虚拟滚动；
- 高频消息、断线、取消、卸载清理和 5k 测点压力测试。

门禁：M1 的 REST/WS/Excel 契约保持不变；SCADA 导入导出、任务启停、断线重连、
历史图和虚拟滚动全绿，浏览器无 timer/socket 泄漏或阻断错误。仍不切默认入口。

### M5：Vue 3 全量切换与回滚

目标：在 8 条路由全量对等后，只切换静态前端入口。

交付物：

- Vue 单测、浏览器全流程和真后端 round-trip；
- Nginx、Compose、离线 Web 镜像与文档改为 Vue 产物；
- React 旧镜像、产物和入口回退说明；
- 切换前后构建体积、页面 p95 和控制台错误对比。

门禁：一次性数据库全流程通过，82 个既有 React 测试场景的业务语义全部保留；只切
静态前端，不改变 Django API、schema 或采集 worker。部署切换必须是单独原子提交，
一笔 revert 即可恢复 React。

### M6：FastAPI 壳、认证与只读兼容层

目标：FastAPI 在独立端口建立生产级骨架，以只读影子流量与 Django 响应对拍。

交付物：

- FastAPI 配置、健康检查、生命周期、结构化日志、异常信封和 OpenAPI；
- 与现有认证/RBAC 等价的依赖与拒绝路径；
- Pydantic schema、只读 repository 和显式 SQLite 表/字段映射；
- 设备、测点、任务、会话、历史、告警等只读接口与影子对拍报告。

门禁：FastAPI 不执行 migration 或业务写入；契约对拍覆盖权限、分页、排序、Decimal、
时间和空值。Django 仍是全部写入和 schema owner；sidecar 停止不能影响当前 API 或采集。

### M7：FastAPI 业务写接口与控制面切流

目标：按业务域迁移配置、导入、版本、告警和采集控制写接口。

交付物：

- 每条 mutation 路由的唯一 owner 表；
- CRUD、Excel 导入/导出、配置版本/回滚和告警事务；
- FastAPI 调用既有 Celery task 的适配层；
- 按精确 URL 切换的反向代理配置与逐域回退开关。

门禁：禁止双写和“失败后让另一个后端补写”；一个路由切到 FastAPI 后，Django 同路由
必须从生产入口移除。共存期仍由 Django migration 独占 schema；采集仍由原
`celery-acq` 独占，本阶段不切 worker。

### M8：WebSocket、Influx 与采集运行时解耦

目标：迁移实时推送，并让采集运行时不再依赖 Django ORM、signals 或 Channels。

交付物：

- FastAPI WebSocket + Redis 广播，事件名、帧和重连语义与 M1 契约一致；
- Influx 查询/写入、spill/replay 和历史接口适配；
- repository/service 显式承接采样率重启、删除级联、startup recovery、watchdog、告警
  和幂等守卫；
- Alembic baseline；只有 Django migration 冻结后才能接管 schema。

门禁：Mock 协议全链路、Redis/Influx/设备断连、worker kill 和恢复通过；WebSocket
无异常重复且可重连，采集 schema 不变。采集 worker 演练必须先停旧实例、flush、确认
设备/Broker 连接释放，再启新实例；任何环境不得让新旧采集器连接同一设备或账号。

### M9：移除 Django 运行时、发布 RC 与现场灰度

目标：生产运行不再依赖 Django/DRF/Channels，从 accepted SHA 构建不可变发布物，
完成单站灰度、回滚演练和最终交接。

交付物：

- 运行 Compose/镜像/启动脚本移除 Django 服务和 Django migration 入口；
- FastAPI/Alembic 成为唯一控制面与 schema owner；
- 清理仅供 Django 运行时使用的代码和依赖，保留必要的数据迁移/审计工具；
- 干净 amd64 环境可安装的离线 RC、SBOM、镜像 digest 和恢复证据；
- 24/72 小时稳定性、资源、丢失/重复和现场回滚报告。

发布前门禁：

- M0～M8 的 accepted SHA、CI 和验收记录完整；
- 使用 Git SHA/版本号标记镜像并记录 digest，不只使用可变的 `latest` 或
  `offline-amd64` tag；
- `images.tar.sha256` 正确，manifest 中所有目标镜像均为 `amd64`；
- 在干净 amd64 VM 验证 `docker load`、migrate、full/headless 两种模式；
- 完成 SQLite 一致性备份、InfluxDB backup、配置导出、旧 compose/环境文件与旧镜像
  归档；禁止依赖旧容器仍然存在作为唯一回滚手段。

现场切换顺序：

1. 只加载新镜像并完成依赖、端口、磁盘和备份预检；
2. UI/API 可先灰度，冻结配置写入后再处理采集器；
3. 优雅停止旧采集会话，确认 Influx buffer/spill queue 已 flush；
4. 确认旧 Broker/设备连接释放，随后启动唯一的新采集器；
5. 验证首页、API、Redis、Influx、会话状态、WebSocket、告警和入库计数持续增长；
6. 观察 30～60 分钟后进入 24/72 小时灰度期，期间保留旧镜像和回滚材料；
7. 灰度通过后才关闭旧栈；不得执行 `docker compose down -v`。

立即回滚条件包括：会话无法在约定时间恢复、入库计数不增长、重复采集或异常缺口、
Broker 142 互踢、migration 失败、持续 API 5xx、关键 WS 断流或新增 critical 告警。

回滚顺序：先停止新 worker并禁用自启，确认新设备连接释放；恢复旧路由和旧镜像；
若数据库仅有 expand 变更则旧版应可直接启动，若出现破坏性变更则停止所有写入者后
恢复 SQLite 备份；最后确认只有一个采集客户端且入库计数重新增长。保留失败版本日志、
容器 inspect 和数据样本供复盘。

发布提交与 tag 必须先在 `origin` 完成 CI/评审，再将同一 SHA 推送到 `midea`。两个
远端的 release branch 和 annotated tag SHA 一致，才算 M9 Git 交付完成。

## 5. 完成定义

只有满足以下条件，才能宣告迁移完成：

- Vue 3 已覆盖全部现有用户流程，React 可从运行部署中移除；
- FastAPI 已承接约定 API/WS，生产进程不再依赖 Django；
- 配置、版本、告警、历史和 Influx 数据契约均通过对拍；
- 现场始终满足单写入者和单采集器约束；
- 离线安装、升级、备份和回滚都在目标架构机器真实演练通过；
- `origin` 与 `midea` 保存相同的已验收源码 SHA 和发布 tag。
