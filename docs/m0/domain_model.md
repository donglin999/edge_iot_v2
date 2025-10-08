# 领域模型草案

## 核心实体

| 实体 | 说明 | 关键字段 | 备注 |
| --- | --- | --- | --- |
| `Site` 站点 | 采集系统的组织或厂区 | 名称、位置、网络区域、负责人 | 支撑多租户/分区运维 |
| `Device` 设备 | 被采集对象（PLC、传感器网关等） | 协议类型、IP/端口、厂商、所属站点、状态 | 支持绑定多协议适配器 |
| `Channel` 通道 | 设备上的逻辑/物理通道 | 通道号、寄存器范围、采样频率、系数模板 | 便于复用配置 |
| `PointTemplate` 测点模板 | 定义测点的共性属性 | 英文名、单位、数据类型、缩放/系数、精度 | Excel 字段映射来源 |
| `Point` 测点 | 实际采集的点位 | 关联设备、模板、寄存器地址、采样率、Kafka 标记 | 支持版本化、历史记录 |
| `AcqTask` 采集任务 | 逻辑上的采集计划 | 名称、描述、执行计划（立即/定时）、关联测点集合、责任人 | 对应前端任务管理 |
| `TaskRun` 任务实例 | 每次执行的运行记录 | 状态、开始/结束时间、持续时长、worker 节点、日志引用 | 支持任务追溯 |
| `WorkerEndpoint` 采集节点 | 承载协议 worker 的执行环境 | 节点 ID、主机信息、心跳、运行协议列表 | 可与边缘 Agent 关联 |
| `ImportJob` 导入作业 | Excel 导入/导出的操作记录 | 文件路径、发起人、时间、校验结果、变更 diff | 支持回滚与审计 |
| `ConfigVersion` 配置版本 | 存档发布的配置快照 | 版本号、生成时间、基线说明、关联任务/测点 | 回滚依据 |
| `AlarmRule` 告警规则 | 可选功能，定义阈值/逻辑 | 关联测点、触发条件、通知策略 | 当前阶段可留空 |
| `AlarmEvent` 告警事件 | 规则触发结果 | 触发时间、关联任务、处理状态 | 供后续扩展 |

## 关系草图
- `Site` 1 — n `Device`
- `Device` 1 — n `Channel`
- `Channel` 1 — n `Point`
- `Point` n — n `AcqTask`（通过 `TaskPoint` 关联表）
- `AcqTask` 1 — n `TaskRun`
- `AcqTask` 1 — n `ConfigVersion`
- `ImportJob` 1 — n `ConfigVersion`
- `WorkerEndpoint` 1 — n `TaskRun`
- `AlarmRule` n — n `Point`

## 配置生命周期
1. **导入准备**：运维上传 Excel → 系统在沙箱环境解析（`ImportJob` 创建为 `PENDING`）。
2. **语义校验**：映射到实体模型，检查字段完整性、数据类型、协议约束；记录校验报告。
3. **变更比对**：与当前生效 `ConfigVersion` 计算 diff，生成待审批项。
4. **审核确认**：根据运维流程（单人/多人）确认变更；可附加备注。
5. **发布入库**：将新配置落库并生成 `ConfigVersion`；与相关 `AcqTask` 建立关系。
6. **同步下发**：任务编排服务读取最新配置 → 生成 Celery 任务 → 通知相关 Worker 热加载。
7. **回滚处理**：若发布失败，可回滚到上一个 `ConfigVersion`，同时记录 `ImportJob` 状态。
8. **导出归档**：支持一键导出当前版本 Excel，以供线下留档或二次编辑。

## Excel 映射原则
- 按协议类型 + `source_ip` + `source_port` 分组，对应 `Device` + `Channel`。
- 测点行映射至 `PointTemplate` + `Point`；保留 Kafka、系数、单位等字段。
- Excel 内新增列需维护映射表，避免硬编码；版本化后可在后台管理。

## 数据完整性规则
- 不允许重复的 `Point`（同设备 + 地址 + 测点名）。
- `AcqTask` 至少关联一个 `Point`，且所有点需处于同站点或允许跨站点策略。
- `TaskRun` 必须引用触发该运行的 `ConfigVersion`，实现可追溯。
- 所有外部键启用级联软删除，历史版本保留只读。
