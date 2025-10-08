# 架构蓝图 v1

## 系统上下文

```mermaid
flowchart LR
    subgraph Frontend[前端]
        A[Web SPA/运维控制台]
    end
    subgraph Django[控制平面 API]
        B[Django REST API / GraphQL]
        C[配置管理服务]
        D[任务编排服务]
        E[数据访问服务]
    end
    subgraph Workers[采集执行层]
        F[协议 Worker (Modbus/PLC/MQTT/VK…)]
        G[采集 Agent / 边缘节点]
    end
    subgraph Storage[数据与状态]
        H[(PostgreSQL/SQLite 配置库)]
        I[(Redis Broker/结果缓存)]
        J[(InfluxDB 时序库)]
        K[(对象存储/文件库)]
    end
    subgraph External[外部系统]
        L[Kafka / 下游系统]
        M[可选告警平台]
    end

    A <-->|REST/WS/Upload| B
    B --> C
    B --> D
    B --> E
    C <-->|ORM| H
    C --> K
    D <-->|任务调度| I
    D -->|执行指令| F
    F -->|采集结果| J
    F -->|向下游推送| L
    E -->|查询| J
    D -->|任务状态| I
    I --> B
    B -->|进度通知| A
    J -->|可选指标| M
```

## 组件职责

- **Web SPA**：负责配置管理、任务调度、运维可视化、日志查询、文件上传。
- **Django API 网关**：统一认证（当前为“无鉴权”占位）、路由、速率限制、API 文档、异常兜底。
- **配置管理服务**：承载 Excel 导入解析、配置版本管理、差异比对、发布与回滚。
- **任务编排服务**：依托 Celery + Redis 调度协议 worker，提供任务生命周期管理、重试、并发控制。
- **数据访问服务**：封装 InfluxDB 查询、聚合、导出接口，并支持未来扩展到其他时序/告警平台。
- **协议 Worker**：对接各类工业协议，负责数据采集、转换、写入 InfluxDB；Kafka 推送作为可选扩展能力。
- **采集 Agent**：可选部署在边缘，负责 worker 进程守护、健康上报、日志落地。
- **Redis Broker**：承载任务队列与状态缓存，兼顾在线任务进度订阅。
- **配置数据库**：存储站点、设备、测点、任务等结构化数据，初期可用 SQLite，后续切换 PostgreSQL。
- **对象存储**：保存 Excel 原始文件、导入报告、导出配置；也可用本地文件系统。
- **InfluxDB**：存储采集数据，提供历史查询、窗口聚合；保留策略按业务需求制定。
- **Kafka**：当前阶段不启用；保留接口以支撑未来扩展。（意见：__________________）
- **外部告警平台**：当前不需要对接，预留标准接口。（意见：__________________）

## 关键流程

1. 运维人员通过前端上传 Excel → Django 校验 → 存库 → 生成导入报告。
2. 运维在前端创建/修改采集任务 → Django 下发至 Celery → Worker 加载对应协议配置 → 与设备交互 → 数据入 InfluxDB。
3. Django 通过 Redis/SSE 推送任务状态、运行日志至前端。
4. 前端按需发起数据查询 → Django 数据服务层转换查询语义 → InfluxDB 返回结果。
5. Kafka 集成：当前阶段确认 **不做推送**；如未来启用，可由 Worker 同步或异步写入低频数据。（意见：__________________）

## 非功能考量

- **可扩展性**：任务编排与 Worker 分布式部署，支持水平扩展；Redis 作为 Broker，后续可切换 RabbitMQ/其他实现。
- **稳定性**：Worker 采用心跳+重试机制；Django 通过健康检查接口确认 Agent/Worker 状态。
- **安全性**：部署环境为内网，阶段内无需启用 HTTPS，仅保留后续接入 JWT/SSO 的扩展点。（意见：__________________）
- **可观测性**：统一结构化日志（JSON）落地；Prometheus 暴露任务指标；InfluxDB 提供数据层监控观察点。
- **部署模式**：生产建议使用 Docker Compose/Kubernetes；开发阶段可在物理机直接运行核心组件。（意见：__________________）
