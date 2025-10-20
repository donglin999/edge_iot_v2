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

- **后端**: Django 4.2 + Django REST Framework
- **任务队列**: Celery + Redis
- **时序数据库**: InfluxDB 2.x
- **关系数据库**: SQLite3
- **前端**: React + TypeScript + Vite

## 快速开始

### 1. 环境要求

- Python 3.10+
- Node.js 18+
- Docker & Docker Compose
- Redis 7+

### 2. 启动基础服务

```bash
# 启动Docker服务（Redis, InfluxDB）
docker-compose up -d

# 检查服务状态
docker ps
```

### 3. 后端服务

```bash
cd backend

# 安装依赖
pip install -r requirements.txt

# 数据库迁移
python3 manage.py migrate

# 启动Django服务
python3 manage.py runserver 0.0.0.0:8000
```

### 4. Celery Worker

```bash
cd backend

# 启动Celery Worker
celery -A control_plane worker -l info --pool=solo
```

### 5. 前端服务

```bash
cd frontend

# 安装依赖（首次）
npm install

# 启动开发服务器
npm run dev
```

### 6. 一键启动（推荐）

```bash
# 启动所有服务
./start_services.sh

# 访问前端
# http://localhost:5173

# 访问后端API
# http://localhost:8000/api/
```

## 系统架构

详见 [系统架构文档](docs/SYSTEM_ARCHITECTURE.md)

### 核心模块

```
backend/
├── acquisition/        # 数据采集模块
│   ├── models.py       # 采集会话模型
│   ├── services/       # 采集服务（持久连接、批量上传）
│   ├── tasks.py        # Celery后台任务
│   └── views.py        # API接口
├── configuration/      # 配置管理模块
│   ├── models.py       # 设备、测点、任务模型
│   ├── services/       # Excel导入服务
│   └── views.py        # API接口
├── storage/           # 存储模块
│   └── influxdb.py    # InfluxDB存储实现
├── protocols/         # 协议适配模块
│   ├── modbus_tcp.py  # Modbus TCP协议
│   ├── melsec.py      # MELSEC A1E协议
│   └── mqtt.py        # MQTT协议
└── control_plane/     # Django配置
    ├── settings.py    # 项目设置
    └── celery.py      # Celery配置

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

在 `backend/.env` 中配置：

```bash
# Django
SECRET_KEY=your-secret-key
DEBUG=True

# Redis
REDIS_HOST=localhost
REDIS_PORT=6379

# InfluxDB
INFLUXDB_HOST=localhost
INFLUXDB_PORT=8086
INFLUXDB_TOKEN=my-super-secret-auth-token
INFLUXDB_ORG=edge-iot
INFLUXDB_BUCKET=iot-data
INFLUXDB_CONTAINER_NAME=influxdb

# 采集服务配置
ACQUISITION_BATCH_SIZE=50                    # 批量大小
ACQUISITION_BATCH_TIMEOUT=5.0                # 批量超时（秒）
ACQUISITION_CONNECTION_TIMEOUT=30.0          # 连接超时（秒）
ACQUISITION_MAX_RECONNECT_ATTEMPTS=3         # 最大重连次数
```

### Docker Compose

主要服务配置在 `docker-compose.yml`：

- Redis: `localhost:6379`
- InfluxDB: `localhost:8086`

## 测试

### 后端测试

```bash
cd backend

# 运行所有测试
python3 -m pytest tests/ -v

# 运行特定测试
python3 -m pytest tests/test_protocols.py -v
```

### Mock设备

```bash
# 启动Modbus TCP模拟设备
python3 mock/modbus_tcp_mock.py --port 5020
```

## 监控与运维

### 查看日志

```bash
# Django日志
tail -f logs/django_final.log

# Celery日志
tail -f logs/celery_final.log
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

1. 在 `backend/protocols/` 下创建新协议文件
2. 继承 `BaseProtocol` 接口
3. 实现 `connect()`, `disconnect()`, `read_points()` 方法
4. 在 `ProtocolRegistry` 中注册

示例:
```python
from backend.protocols.base import BaseProtocol

class CustomProtocol(BaseProtocol):
    def connect(self):
        # 实现连接逻辑
        pass

    def disconnect(self):
        # 实现断开逻辑
        pass

    def read_points(self, points):
        # 实现读取逻辑
        return readings

# 注册协议
from backend.protocols.registry import ProtocolRegistry
ProtocolRegistry.register("custom", CustomProtocol)
```

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

## 项目结构

```
edge_iot_v2/
├── backend/                # Django后端
│   ├── acquisition/       # 采集模块
│   ├── configuration/     # 配置模块
│   ├── storage/          # 存储模块
│   ├── protocols/        # 协议模块
│   ├── common/           # 公共模块
│   ├── monitoring/       # 监控模块
│   └── control_plane/    # Django配置
├── frontend/             # React前端
│   ├── src/
│   │   ├── components/  # 组件
│   │   ├── pages/       # 页面
│   │   ├── services/    # API服务
│   │   └── hooks/       # Hooks
├── mock/                # Mock设备
├── docs/                # 文档
├── logs/                # 日志文件
├── docker-compose.yml   # Docker配置
├── start_services.sh    # 启动脚本
└── README.md           # 本文件
```

## 贡献指南

欢迎提交Issue和Pull Request！

## 许可证

MIT License

## 联系方式

如有问题，请提交Issue或联系项目维护者。
