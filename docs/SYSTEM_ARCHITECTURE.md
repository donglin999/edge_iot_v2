# 边缘IoT数据采集系统架构文档

## 系统概述

边缘IoT数据采集系统是一个基于Django + Celery的分布式数据采集平台，支持多种工业协议（Modbus TCP、MELSEC A1E、MQTT等），实现设备数据的实时采集、存储和可视化。

## 核心架构

### 技术栈

- **后端框架**: Django 4.2 + Django REST Framework
- **任务队列**: Celery + Redis
- **时序数据库**: InfluxDB 2.x
- **关系数据库**: SQLite3
- **前端框架**: React + TypeScript + Vite
- **工业协议**: Modbus TCP, MELSEC A1E, MQTT

### 系统架构图

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│                 │     │                  │     │                 │
│  React Frontend │────▶│  Django REST API │────▶│  SQLite3        │
│  (Port 5173)    │     │  (Port 8000)     │     │  (配置数据)     │
│                 │     │                  │     │                 │
└─────────────────┘     └──────────────────┘     └─────────────────┘
                               │
                               ▼
                        ┌──────────────┐
                        │              │
                        │  Celery      │
                        │  Worker      │
                        │  (后台采集)   │
                        │              │
                        └──────────────┘
                               │
                    ┌──────────┴──────────┐
                    ▼                     ▼
            ┌──────────────┐      ┌─────────────┐
            │              │      │             │
            │  Redis       │      │  InfluxDB   │
            │  (消息队列)   │      │  (时序数据)  │
            │              │      │             │
            └──────────────┘      └─────────────┘
                                         ▲
                                         │
                                  ┌──────┴───────┐
                                  │              │
                                  │  工业设备     │
                                  │  (Modbus等)  │
                                  │              │
                                  └──────────────┘
```

## 核心功能模块

### 1. 配置管理模块 (configuration)

负责设备、测点、任务的配置管理和Excel导入。

**主要模型**:
- `Device`: 设备配置（协议、IP地址、端口等）
- `Point`: 测点配置（寄存器地址、数据类型、单位等）
- `Task`: 采集任务配置（采集周期、关联设备等）
- `ImportJob`: Excel导入任务

**核心服务**:
- `ExcelImporter`: Excel文件解析和配置导入
- 支持设备、测点、任务的批量导入
- 自动创建版本管理

### 2. 数据采集模块 (acquisition)

实现持久连接、批量上传、健康监控的数据采集服务。

**主要模型**:
- `AcquisitionSession`: 采集会话
  - 状态: `running`, `paused`, `stopped`, `error`（无中间状态）
  - 元数据: 设备健康状态、错误信息、性能指标

**核心服务**: `AcquisitionService`

#### 持久连接机制

采集服务采用持久连接设计，避免频繁建立/断开连接：

```python
# 初始化阶段 - 建立所有设备连接
for device_id, group in device_groups.items():
    protocol = ProtocolRegistry.create(device.protocol, config)
    protocol.connect()  # 只连接一次
    device_protocols[device_id] = protocol

# 采集循环 - 复用连接读取数据
while session.status == STATUS_RUNNING:
    for device_id, group in device_groups.items():
        protocol = device_protocols[device_id]
        readings = protocol.read_points(points)  # 不断开连接
        batch_buffer.extend(readings)

    # 批量写入
    if len(batch_buffer) >= batch_size or elapsed >= batch_timeout:
        storage.write(batch_buffer)

# 清理阶段 - 断开所有连接
for protocol in device_protocols.values():
    protocol.disconnect()
```

#### 批量上传机制

配置参数（settings.py）:
- `ACQUISITION_BATCH_SIZE`: 批量大小（默认50个点）
- `ACQUISITION_BATCH_TIMEOUT`: 批量超时（默认5秒）

满足任一条件即触发写入：
1. 缓冲区达到50个数据点
2. 距离上次写入超过5秒

#### 健康监控机制

**监控指标**:
- `last_success`: 最后成功时间戳
- `consecutive_failures`: 连续失败次数
- `status`: 设备状态（healthy/error/timeout/disconnected）

**超时检测**:
- `ACQUISITION_CONNECTION_TIMEOUT`: 连接超时（默认30秒）
- 如果30秒内无成功读取，标记为`timeout`

**自动重连**:
- `ACQUISITION_MAX_RECONNECT_ATTEMPTS`: 最大重试次数（默认3次）
- 指数退避策略: 1秒, 2秒, 4秒

```python
# 健康监控逻辑
try:
    readings = protocol.read_points(points)
    device_health[device_id]["last_success"] = time.time()
    device_health[device_id]["consecutive_failures"] = 0
    device_health[device_id]["status"] = "healthy"
except Exception as e:
    device_health[device_id]["consecutive_failures"] += 1

    # 超时检测
    if (time.time() - last_success) > connection_timeout:
        device_health[device_id]["status"] = "timeout"
        # 触发重连
        protocol.disconnect()
        device_protocols[device_id] = None
```

#### 同步启动验证

启动采集任务时进行5秒同步验证：

```python
@action(detail=False, methods=['post'])
def start_task(self, request):
    TIMEOUT = 5.0  # 5秒超时

    # 验证所有设备连接和测点
    for device in devices:
        protocol.connect()
        readings = protocol.read_points(points)

        validation_results[device.code] = {
            "status": "healthy" if all_success else "partial",
            "connected": True,
            "successful_points": len(readings),
            "failed_points": len(points) - len(readings)
        }
        protocol.disconnect()

    # 启动后台任务
    celery_result = start_acquisition_task.delay(task_id)

    return Response({
        "detail": "任务启动成功" if all_healthy else "部分测点异常",
        "validation": validation_results,
        "elapsed_seconds": elapsed
    })
```

**返回状态**:
- `all_healthy`: 所有测点正常
- `partial`: 部分测点异常（设备连接正常但部分点不存在）
- `error`: 设备无法连接

### 3. 数据存储模块 (storage)

支持InfluxDB 2.x时序数据存储。

**核心类**: `InfluxDBStorage`

#### 双写入策略

由于WSL2环境下InfluxDB认证问题，实现了双写入策略：

1. **HTTP API写入**（主方式）
   ```python
   write_api.write(bucket, org, points, write_precision=WritePrecision.NS)
   ```

2. **Docker Exec写入**（回退方式）
   ```python
   docker exec -i influxdb influx write \
     -b iot-data -o edge-iot -t <token> < line_protocol
   ```

#### Line Protocol格式

```
measurement,tag1=value1,tag2=value2 field1=value1,field2=value2 timestamp
```

**标签转义**:
- 空格: `\ `
- 逗号: `\,`
- 等号: `\=`

**字段值规则**:
- 整数: 后缀`i` (如 `123i`)
- 浮点数: 直接写 (如 `12.34`)
- 字符串: 双引号包裹 (如 `"value"`)
- 数组: JSON字符串 (如 `"[1,2,3]"`)

### 4. 协议适配模块 (protocols)

**协议注册表**: `ProtocolRegistry`

支持的协议:
- `modbus_tcp`: Modbus TCP协议
- `melsec_a1e`: 三菱MELSEC A1E协议
- `mqtt`: MQTT协议

**协议接口**:
```python
class BaseProtocol:
    def connect(self) -> None
    def disconnect(self) -> None
    def read_points(self, points: List[Point]) -> List[Dict]
    def write_point(self, point: Point, value: Any) -> bool
```

### 5. Mock设备模块 (mock)

提供Modbus TCP模拟设备用于测试。

**特性**:
- 支持多种寄存器类型（保持寄存器、输入寄存器等）
- 自动生成随机数据或固定值
- 支持数组类型测点
- 可配置端口（默认5020）

## API接口设计

### 采集控制接口

#### 启动任务
```
POST /api/acquisition/sessions/start-task/
{
  "task_id": 1
}

Response:
{
  "detail": "任务启动成功",
  "validation": {
    "all_healthy": true,
    "total_points": 20,
    "failed_points_count": 0,
    "device_results": {
      "device-code": {
        "status": "healthy",
        "connected": true,
        "total_points": 20,
        "successful_points": 20,
        "failed_points": 0
      }
    }
  },
  "session_id": 123,
  "elapsed_seconds": 2.34
}
```

#### 停止任务
```
POST /api/acquisition/sessions/{id}/stop/

Response:
{
  "detail": "任务已停止",
  "session_id": 123,
  "duration_seconds": 3600.5
}
```

#### 查询活动会话
```
GET /api/acquisition/sessions/active/

Response:
[
  {
    "id": 123,
    "task_code": "task-001",
    "task_name": "设备采集任务",
    "status": "running",
    "duration_seconds": 3600.5,
    "metadata": {
      "device_health": {
        "device-code": {
          "status": "healthy",
          "consecutive_failures": 0,
          "last_success": 1760198567.689
        }
      }
    }
  }
]
```

#### 查询历史数据
```
GET /api/acquisition/sessions/point-history/
  ?point_code=temperature1
  &start_time=-1h
  &limit=100

Response:
{
  "point_code": "temperature1",
  "count": 100,
  "data": [
    {
      "time": "2025-10-12T00:00:00Z",
      "value": 25.5,
      "quality": "good",
      "device": "device-001",
      "site": "factory-a"
    }
  ]
}
```

### 配置管理接口

#### Excel导入
```
POST /api/config/import/excel/
Content-Type: multipart/form-data

file: <excel-file>

Response:
{
  "job_id": 456,
  "status": "completed",
  "summary": {
    "devices_created": 5,
    "points_created": 100,
    "tasks_created": 3
  }
}
```

## 状态机设计

### 采集会话状态

```
        start_task()
            │
            ▼
    ┌───────────────┐
    │               │
    │    RUNNING    │◀────┐ resume()
    │               │     │
    └───────────────┘     │
            │             │
            │ pause()     │
            ▼             │
    ┌───────────────┐     │
    │               │     │
    │    PAUSED     │─────┘
    │               │
    └───────────────┘
            │
            │ stop()
            ▼
    ┌───────────────┐
    │               │
    │    STOPPED    │
    │               │
    └───────────────┘

    任何状态遇到致命错误 ──▶ ERROR
```

**状态说明**:
- `RUNNING`: 正在采集数据
- `PAUSED`: 暂停采集（保持连接）
- `STOPPED`: 已停止（断开连接）
- `ERROR`: 错误状态（需要重启）

**重要**: 无`STARTING`和`STOPPING`中间状态，启动和停止都是同步快速完成。

## 配置参数

### Django设置 (settings.py)

```python
# 采集服务配置
ACQUISITION_BATCH_SIZE = 50              # 批量大小
ACQUISITION_BATCH_TIMEOUT = 5.0          # 批量超时（秒）
ACQUISITION_CONNECTION_TIMEOUT = 30.0    # 连接超时（秒）
ACQUISITION_MAX_RECONNECT_ATTEMPTS = 3   # 最大重连次数

# InfluxDB配置
INFLUXDB_HOST = "localhost"
INFLUXDB_PORT = 8086
INFLUXDB_TOKEN = "my-super-secret-auth-token"
INFLUXDB_ORG = "edge-iot"
INFLUXDB_BUCKET = "iot-data"
INFLUXDB_CONTAINER_NAME = "influxdb"     # Docker容器名

# Redis配置
REDIS_HOST = "localhost"
REDIS_PORT = 6379
```

### Celery配置

```python
CELERY_BROKER_URL = 'redis://localhost:6379/0'
CELERY_RESULT_BACKEND = 'redis://localhost:6379/0'
CELERY_TASK_SERIALIZER = 'json'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TIMEZONE = 'Asia/Shanghai'
CELERY_TASK_TRACK_STARTED = True
```

## 启动流程

### 1. 启动基础服务

```bash
# 启动Docker服务（Redis, InfluxDB）
docker-compose up -d

# 检查服务状态
docker ps
```

### 2. 启动Django服务

```bash
cd backend

# 数据库迁移
python3 manage.py migrate

# 启动开发服务器
python3 manage.py runserver 0.0.0.0:8000
```

### 3. 启动Celery Worker

```bash
cd backend

celery -A control_plane worker -l info --pool=solo
```

### 4. 启动前端服务

```bash
cd frontend

# 安装依赖（首次）
npm install

# 启动开发服务器
npm run dev
```

### 5. 启动Mock设备（可选）

```bash
python3 mock/modbus_tcp_mock.py --port 5020
```

### 一键启动脚本

```bash
./start_services.sh
```

## 数据流转

### 采集流程

```
1. 用户在前端启动任务
   │
   ▼
2. Django API接收请求，进行5秒同步验证
   │
   ▼
3. 验证通过，创建AcquisitionSession（状态=RUNNING）
   │
   ▼
4. 触发Celery异步任务start_acquisition_task
   │
   ▼
5. AcquisitionService初始化并建立持久连接
   │
   ▼
6. 循环读取数据到批量缓冲区
   │
   ▼
7. 满足条件时批量写入InfluxDB
   │
   ▼
8. 更新设备健康状态到Session元数据
   │
   ▼
9. 前端通过轮询或WebSocket获取实时状态
```

### 数据存储格式

**InfluxDB Measurement**: `{device_code}`

**Tags**:
- `site`: 站点编码
- `device`: 设备编码
- `point`: 测点编码
- `quality`: 数据质量（good/bad/uncertain）
- `cn_name`: 中文名称
- `unit`: 单位

**Fields**:
- `{point_code}`: 测点值（字段名=测点编码）

**Timestamp**: 纳秒精度时间戳

**示例**:
```
modbustcp-device,site=factory-a,device=modbustcp-device,point=temperature1,quality=good,cn_name=温度\ 1,unit=℃ temperature1=25.5 1760198567689000000
```

## 错误处理

### 设备连接错误

**问题**: 设备无法连接
**处理**:
1. 标记设备状态为`error`
2. 记录错误信息到Session
3. 继续尝试其他设备
4. 达到重试上限后标记Session为ERROR

### InfluxDB写入错误

**问题**: InfluxDB写入失败
**处理**:
1. 首先尝试HTTP API写入
2. 失败则尝试Docker Exec回退
3. 两种方式都失败则记录错误日志
4. 不中断采集流程

### 测点读取错误

**问题**: 单个测点读取失败
**处理**:
1. 标记该测点为失败
2. 继续读取其他测点
3. 在验证报告中体现`partial`状态

## 性能优化

### 1. 批量操作
- 数据批量写入InfluxDB（50点/批）
- 测点批量读取（同设备测点合并请求）

### 2. 连接复用
- 持久TCP连接（避免频繁建立/断开）
- 连接池管理（支持多设备并发）

### 3. 异步处理
- Celery异步任务（不阻塞API响应）
- 后台采集循环（独立进程）

### 4. 缓存策略
- Redis缓存设备配置
- 配置版本快照（避免重复查询）

## 监控指标

### 采集性能指标

- **采集速率**: 点/秒
- **批量效率**: 平均批量大小
- **连接稳定性**: 设备在线率
- **数据质量**: 成功率/失败率

### 系统健康指标

- **API响应时间**: p50, p95, p99
- **Celery队列长度**: 待处理任务数
- **InfluxDB写入延迟**: 写入耗时
- **设备健康状态**: healthy/error/timeout分布

## 安全考虑

### 1. 认证授权
- Django REST Framework Token认证
- 基于角色的权限控制

### 2. 数据验证
- 输入数据校验（serializers）
- 配置合法性检查

### 3. 错误隔离
- 设备错误不影响其他设备
- 采集错误不影响API服务

## 扩展性设计

### 1. 协议扩展
实现`BaseProtocol`接口即可添加新协议：

```python
class CustomProtocol(BaseProtocol):
    def connect(self):
        # 实现连接逻辑
        pass

    def read_points(self, points):
        # 实现读取逻辑
        pass

# 注册协议
ProtocolRegistry.register("custom", CustomProtocol)
```

### 2. 存储扩展
实现`BaseStorage`接口即可添加新存储：

```python
class CustomStorage(BaseStorage):
    def write(self, points):
        # 实现写入逻辑
        pass

    def query(self, measurement, start_time, end_time):
        # 实现查询逻辑
        pass
```

### 3. 分布式部署
- Celery支持多Worker横向扩展
- Redis Cluster支持高可用
- InfluxDB支持集群部署

## 故障排查

### 常见问题

**1. InfluxDB认证失败 (401 Unauthorized)**
- 检查Token配置
- 使用Docker Exec回退方式

**2. 设备连接超时**
- 检查网络连通性
- 验证设备地址和端口
- 查看防火墙设置

**3. Celery任务不执行**
- 检查Redis连接
- 验证Celery Worker运行状态
- 查看Celery日志

**4. 前端无法连接后端**
- 检查Django服务状态
- 验证CORS配置
- 查看浏览器控制台错误

### 日志位置

- Django日志: `logs/django_final.log`
- Celery日志: `logs/celery_final.log`
- 采集服务日志: 通过Django logger输出

## 开发规范

### 代码结构

```
backend/
├── acquisition/          # 采集模块
│   ├── models.py        # 数据模型
│   ├── services/        # 业务逻辑
│   ├── tasks.py         # Celery任务
│   └── views.py         # API视图
├── configuration/       # 配置模块
├── storage/            # 存储模块
├── protocols/          # 协议模块
└── control_plane/      # Django配置

frontend/
├── src/
│   ├── components/     # React组件
│   ├── pages/          # 页面组件
│   ├── services/       # API服务
│   └── hooks/          # 自定义Hooks
```

### Git工作流

- `master`: 主分支（稳定版本）
- `develop`: 开发分支
- `feature/*`: 功能分支
- `fix/*`: 修复分支

### 测试策略

- 单元测试: pytest
- 集成测试: Django TestCase
- Mock测试: 使用Mock设备

## 未来规划

### 短期目标
- [ ] 添加数据可视化大屏
- [ ] 实现告警规则引擎
- [ ] 支持更多工业协议（OPC UA、Profinet等）

### 长期目标
- [ ] 边缘计算能力（本地数据处理）
- [ ] AI异常检测
- [ ] 移动端App

## 总结

本系统采用持久连接 + 批量上传 + 健康监控的架构设计，实现了高效稳定的工业数据采集。核心特点：

1. **持久连接**: 减少连接开销，提高采集效率
2. **批量上传**: 降低网络请求，优化写入性能
3. **同步验证**: 5秒快速反馈，无中间状态
4. **健康监控**: 实时监控设备状态，自动重连
5. **双写策略**: 解决WSL2认证问题，保证数据可靠写入
6. **协议可扩展**: 支持多种工业协议，易于扩展
