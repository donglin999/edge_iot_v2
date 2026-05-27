# 边缘IoT数据采集系统

一个基于Django + Celery + InfluxDB的工业数据采集平台，支持Modbus TCP、MELSEC A1E、MQTT等多种工业协议。

## 核心特性

- **持久连接**: 保持设备连接，避免频繁建立/断开，提高采集效率
- **批量上传**: 数据批量写入InfluxDB，优化存储性能
- **健康监控**: 实时监控设备状态，自动重连，超时检测
- **同步验证**: 5秒快速启动验证，无中间状态
- **协议可扩展**: 支持多种工业协议，易于扩展新协议
- **Web可视化**: 实时查看采集状态和历史数据

## 技术栈

- **后端**: Django 4.2 + Django REST Framework + Channels (WebSocket)
- **任务队列**: Celery + Redis
- **时序数据库**: InfluxDB 2.x
- **关系数据库**: SQLite3
- **前端**: React 18 + TypeScript + Vite + Ant Design 5
- **部署**: Docker Compose 一键启动

## 快速开始

### 环境要求

- Docker & Docker Compose

### 一键启动（全栈 Docker）

```bash
# 启动所有服务（Django、Celery、前端、Redis、InfluxDB、Mock 设备）
docker compose up -d

# 查看运行状态
docker compose ps

# 查看日志
docker compose logs -f django
docker compose logs -f celery
docker compose logs -f frontend
```

服务启动后访问：

- 前端: http://localhost:5173
- 后端 API: http://localhost:8000/api/
- API 文档: http://localhost:8000/api/schema/swagger-ui/
- InfluxDB UI: http://localhost:8086
- Mock Modbus TCP: localhost:5020
- Mock MQTT Broker: localhost:1883

### 常用命令

```bash
# 停止所有服务
docker compose down

# 重启某个服务
docker compose restart django

# 重新构建镜像（修改 Dockerfile 或 requirements.txt 后）
docker compose build

# 进入容器执行命令（如数据库迁移）
docker compose exec django python manage.py migrate
docker compose exec django python manage.py createsuperuser
```

## 系统架构

详见 [系统架构文档](docs/SYSTEM_ARCHITECTURE.md)

### 核心模块

```
backend/
├── acquisition/                 # 数据采集模块
│   ├── models.py                # 采集会话模型
│   ├── protocols/               # 协议适配（Modbus、MQTT、OPC UA、S7 等）
│   ├── services/                # 采集服务（持久连接、批量上传）
│   ├── tasks.py                 # Celery后台任务
│   ├── consumers.py             # WebSocket 实时推送
│   └── views.py                 # API接口
├── configuration/               # 配置管理模块
│   ├── models.py                # 设备、测点、任务模型
│   ├── services/                # Excel导入服务
│   └── views.py                 # API接口
├── storage/                     # 存储模块
│   └── influxdb.py              # InfluxDB存储实现
├── monitoring/                  # 监控指标
└── control_plane/               # Django配置
    ├── settings.py              # 项目设置
    ├── celery.py                # Celery配置
    └── asgi.py                  # ASGI（Channels）入口

frontend/
├── src/
│   ├── components/    # React组件
│   ├── pages/         # 页面组件
│   ├── services/      # API服务
│   └── hooks/         # 自定义Hooks
```

## API文档

### 采集控制

#### 启动任务
```bash
POST /api/acquisition/sessions/start-task/
{
  "task_id": 1
}
```

#### 停止任务
```bash
POST /api/acquisition/sessions/{id}/stop/
```

#### 查询活动会话
```bash
GET /api/acquisition/sessions/active/
```

#### 查询历史数据
```bash
GET /api/acquisition/sessions/point-history/?point_code=temperature1&start_time=-1h&limit=100
```

### 配置管理

#### Excel导入
```bash
POST /api/config/import/excel/
Content-Type: multipart/form-data

file: <excel-file>
```

#### 查询设备列表
```bash
GET /api/config/devices/
```

#### 查询测点列表
```bash
GET /api/config/points/
```

## 配置说明

### 环境变量

容器内默认配置已写入 `docker-compose.yml`（service hostname：`redis-iot`、`influxdb-iot`），开箱即用。

如需覆盖，可创建 `backend/.env`：

```bash
# Django
SECRET_KEY=your-secret-key
DEBUG=True

# 采集服务配置
ACQUISITION_BATCH_SIZE=50                    # 批量大小
ACQUISITION_BATCH_TIMEOUT=5.0                # 批量超时（秒）
ACQUISITION_CONNECTION_TIMEOUT=30.0          # 连接超时（秒）
ACQUISITION_MAX_RECONNECT_ATTEMPTS=3         # 最大重连次数
```

### Docker Compose 服务清单

| Service       | 端口          | 说明              |
| ------------- | ------------- | ----------------- |
| `django`      | 8000          | Django + DRF API  |
| `celery`      | -             | 后台任务 Worker   |
| `frontend`    | 5173          | Vite 开发服务器   |
| `redis`       | 6379          | Celery broker     |
| `influxdb`    | 8086          | 时序存储          |
| `mock-modbus` | 5020          | Mock Modbus TCP   |
| `mock-mqtt`   | 1883 / 9001   | Mock MQTT Broker  |

## 测试

### 后端测试

```bash
# 在 django 容器内运行 pytest
docker compose exec django python -m pytest tests/ -v

# 运行特定测试
docker compose exec django python -m pytest tests/test_acquisition_service.py -v
```

### Mock 设备

Mock Modbus TCP（端口 5020）和 Mock MQTT Broker（1883/9001）随 `docker compose up` 自动启动，无需单独运行。

## 监控与运维

### 查看日志

```bash
docker compose logs -f django
docker compose logs -f celery
docker compose logs -f frontend
```

### 查看采集状态

访问前端: http://localhost:5173

- 采集控制页面: 查看任务状态、设备健康
- 数据可视化页面: 查看历史数据和趋势图

### 健康检查

```bash
# 检查Django服务
curl http://localhost:8000/api/

# 检查活动会话
curl http://localhost:8000/api/acquisition/sessions/active/

# 检查InfluxDB
curl http://localhost:8086/health
```

## 开发指南

### 添加新协议

1. 在 `backend/acquisition/protocols/` 下创建新协议文件
2. 继承 `BaseProtocol` 接口
3. 实现 `connect()`, `disconnect()`, `read_points()` 方法
4. 在 `acquisition/protocols/__init__.py` 中注册到 `ProtocolRegistry`

### 添加新存储

1. 在 `backend/storage/` 下创建新存储文件
2. 继承 `BaseStorage` 接口
3. 实现 `write()`, `query()` 方法

## 故障排查

### InfluxDB认证失败

如果遇到 `401 Unauthorized` 错误，系统会自动使用Docker Exec回退方式写入。

**解决方案**:
- 检查 `INFLUXDB_TOKEN` 配置
- 确认 InfluxDB 容器运行正常
- 验证 bucket 和 org 配置正确

### 设备连接超时

如果设备连接超时，检查：
- 设备IP地址和端口配置
- 网络连通性
- 防火墙设置
- 设备是否在线

### Celery任务不执行

如果Celery任务不执行，检查：
- Redis服务是否运行
- Celery Worker是否启动
- 查看Celery日志

### 前端无法连接后端

如果前端无法连接，检查：
- Django服务是否运行在 `0.0.0.0:8000`
- CORS配置是否正确
- 浏览器控制台错误信息

## 分布式形态简介

除单机形态外，本仓库 `distributed/main` 分支提供一套 **多工控机 (edge) +
中心 (center)** 的分布式形态：每条产线/工控机本地跑 edge-agent + 本地
InfluxDB + 本地告警判定，原始样本不出工控机；中心 (center) 跑 Django +
前端 + history-proxy，只承担控制面、配置下发、告警汇总和按需历史回查。
edge 通过单条 WS 上报心跳/lifecycle/1Hz 聚合/告警事件，断线期间在 edge
本地 outbox 持久化、重连后按 monotonic_seq 回补。

详细架构、协议、部署、运维：[docs/distributed/README.md](docs/distributed/README.md)

### 单机形态 vs 分布式形态

| 维度 | 单机 (master) | 分布式 (distributed/main) |
|------|---------------|---------------------------|
| 部署节点数 | 1 台 | 1 center + N edge |
| 采集进程 | center 上 Celery worker | 每台 edge 上 edge-agent |
| 时序写入 | center InfluxDB | **每台 edge 本地 InfluxDB**（center 不存全量） |
| 控制面 | HTTP REST | HTTP REST + WS (`/ws/fleet/`) |
| 配置下发 | 直接读 SQLite | center → edge `apply_config` WS 帧 |
| 告警判定 | center 进程内 | 每台 edge 进程内（按 `device_code` 隔离） |
| 历史查询 | center → 本地 InfluxDB | center history-proxy → 对应 edge `:18086` |
| 断网容忍 | 单点 | edge 离线期间继续采，重连后 backfill 无 gap |
| 适用场景 | 单机房 / 小规模 / POC | 多产线 / 多机房 / 现场恶劣网络 |
| 协议版本 | n/a | v0.5 (STABLE) |
| 烟测 / 验收 | `docs/QUICKSTART.md` | `docs/distributed/m{2..7}-*.md` |

## 项目结构

```
edge_iot_v2/
├── backend/                    # Django 后端
│   ├── acquisition/            # 采集模块（含 protocols/）
│   ├── configuration/          # 配置模块
│   ├── storage/                # 存储模块（InfluxDB）
│   ├── common/                 # 公共模块
│   ├── monitoring/             # 监控模块
│   ├── control_plane/          # Django 配置
│   ├── tests/                  # 后端测试
│   └── Dockerfile
├── frontend/                   # React 前端
│   └── src/
│       ├── components/
│       ├── pages/
│       ├── services/
│       └── hooks/
├── mock/                       # Mock 设备（Modbus / MQTT）
├── docs/                       # 文档（架构、部署、API、开发）
├── logs/                       # 日志文件
├── docker-compose.yml          # 全栈编排
├── reset_influxdb.sh           # InfluxDB 重置工具
└── README.md
```

## 贡献指南

欢迎提交Issue和Pull Request！

## 许可证

MIT License

## 联系方式

如有问题，请提交Issue或联系项目维护者。
