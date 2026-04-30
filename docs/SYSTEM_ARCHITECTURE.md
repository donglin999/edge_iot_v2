# 系统架构

## 整体架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                         前端 (React + Vite)                      │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────┐ │
│  │Dashboard │ │Acquisition│ │  Device  │ │  Data    │ │Version │ │
│  │  仪表盘  │ │  控制页面 │ │  列表    │ │  可视化  │ │ 历史  │ │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘ └────────┘ │
└─────────────────────────────────────────────────────────────────┘
                              │ HTTP/WebSocket
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Django REST Framework                        │
│  ┌─────────────────┐    ┌─────────────────┐    ┌──────────────┐  │
│  │  acquisition/   │    │ configuration/  │    │   common/    │  │
│  │  数据采集API     │    │  配置管理API    │    │  公共模块    │  │
│  └─────────────────┘    └─────────────────┘    └──────────────┘  │
│           │                       │                               │
│           ▼                       ▼                               │
│  ┌─────────────────┐    ┌─────────────────┐                      │
│  │ acquisition/    │    │ configuration/  │                      │
│  │ services/       │    │ services/       │                      │
│  │ 采集服务         │    │ 导入服务        │                      │
│  └─────────────────┘    └─────────────────┘                      │
└─────────────────────────────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Celery Worker (异步任务)                       │
│  ┌─────────────────┐    ┌─────────────────┐    ┌──────────────┐  │
│  │ 采集会话管理      │    │ 批量数据写入    │    │ 健康检测    │  │
│  └─────────────────┘    └─────────────────┘    └──────────────┘  │
└─────────────────────────────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────────────────────────────┐
│                         存储层                                   │
│  ┌─────────────────┐                    ┌─────────────────┐     │
│  │   InfluxDB 2.x   │                    │   SQLite 3      │     │
│  │   时序数据       │                    │   配置/元数据   │     │
│  └─────────────────┘                    └─────────────────┘     │
└─────────────────────────────────────────────────────────────────┘

                         基础设施
┌─────────────────────────────────────────────────────────────────┐
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────────────┐     │
│  │  Redis  │  │InfluxDB │  │  Docker │  │  Modbus/MQTT/   │     │
│  │  消息队列│  │  时序库 │  │  容器   │  │  MELSEC 设备   │     │
│  └─────────┘  └─────────┘  └─────────┘  └─────────────────┘     │
└─────────────────────────────────────────────────────────────────┘
```

## 核心模块

### 1. acquisition (数据采集模块)

负责与工业设备通信，采集数据并存储到InfluxDB。

```
backend/acquisition/
├── __init__.py
├── apps.py              # Django应用配置
├── models.py            # AcquisitionSession, DataPoint 模型
├── views.py             # REST API视图
├── serializers.py       # DRF序列化器
├── tasks.py             # Celery异步任务
├── consumers.py         # WebSocket消费者
├── signals.py           # Django信号
├── urls.py              # URL路由
├── routing.py           # WebSocket路由
├── protocols/           # 协议实现
│   ├── __init__.py
│   ├── base.py          # 协议基类
│   ├── modbus.py        # Modbus TCP协议
│   ├── mqtt.py          # MQTT协议
│   └── plc.py           # MELSEC A1E PLC协议
└── services/
    ├── __init__.py
    └── acquisition_service.py  # 采集核心服务
```

**核心功能:**
- `AcquisitionSession`: 采集会话管理，追踪每个任务的运行状态
- `DataPoint`: 采集数据点存储，记录原始数据
- `acquisition_service.py`: 持久连接管理、批量写入、错误重试

### 2. configuration (配置管理模块)

管理设备、测点、任务等配置信息。

```
backend/configuration/
├── __init__.py
├── apps.py
├── models.py            # Device, Point, AcqTask, ImportJob模型
├── views.py             # REST API视图
├── serializers.py       # DRF序列化器
├── tasks.py             # Celery异步任务
├── admin.py             # Django Admin配置
├── urls.py              # URL路由
├── api/
│   ├── __init__.py
│   └── serializers.py   # API专用序列化器
├── migrations/
│   └── ...
└── services/
    ├── __init__.py
    └── importer.py      # Excel导入服务
```

**核心模型:**
- `Device`: 设备信息(IP、端口、协议类型)
- `Point`: 测点信息(地址、数据类型)
- `AcqTask`: 采集任务(关联设备和测点)
- `ImportJob`: 导入任务记录

### 3. storage (存储模块)

```
backend/storage/
├── __init__.py
├── base.py              # BaseStorage抽象基类
└── influxdb.py          # InfluxDB存储实现
```

**接口定义:**
```python
class BaseStorage:
    def connect() -> bool
    def disconnect() -> None
    def write(data: List[Dict]) -> bool
    def query(flux_query: str) -> List[Dict]
    def health_check() -> bool
```

### 4. control_plane (Django配置)

```
backend/control_plane/
├── __init__.py
├── settings.py          # Django配置
├── celery.py            # Celery配置
├── urls.py              # 主URL路由
├── asgi.py              # ASGI配置(支持WebSocket)
└── wsgi.py              # WSGI配置
```

### 5. common (公共模块)

```
backend/common/
├── __init__.py
├── exceptions.py         # 自定义异常
└── logging.py           # 日志配置
```

### 6. monitoring (监控模块)

健康检查与监控功能。

### 7. frontend (前端)

```
frontend/
├── src/
│   ├── App.tsx
│   ├── main.tsx
│   ├── components/
│   │   ├── RealtimeChart.tsx       # 实时图表
│   │   ├── HistoricalTrendChart.tsx # 历史趋势
│   │   └── acquisition/
│   │       └── TaskControlPanel.tsx # 任务控制面板
│   ├── pages/
│   │   ├── DashboardPage.tsx        # 仪表盘
│   │   ├── DeviceListPage.tsx       # 设备列表
│   │   ├── DeviceDetailPage.tsx    # 设备详情
│   │   ├── AcquisitionControlPage.tsx # 采集控制
│   │   ├── DataVisualizationPage.tsx # 数据可视化
│   │   ├── ImportJobPage.tsx       # 导入任务
│   │   └── VersionHistoryPage.tsx  # 版本历史
│   ├── services/
│   │   ├── deviceApi.ts            # 设备API
│   │   ├── acquisitionApi.ts       # 采集API
│   │   ├── dataApi.ts              # 数据API
│   │   └── versionApi.ts          # 版本API
│   └── hooks/
│       └── useWebSocket.ts         # WebSocket Hook
└── package.json
```

## 数据流

```
1. 配置导入流程:
   Excel文件 → /api/config/import/excel/ → importer.py → Device/Point/AcqTask

2. 采集启动流程:
   启动任务 → 创建AcquisitionSession → Celery Task → 协议连接 → 循环采集

3. 数据采集流程:
   设备 → 协议读取 → acquisition_service.py → 批量缓冲 → InfluxDB

4. 实时数据流:
   InfluxDB → Celery查询任务 → WebSocket → 前端图表

5. 历史数据查询:
   前端请求 → /api/acquisition/sessions/point-history/ → InfluxDB查询 → 返回数据
```

## 协议支持

### Modbus TCP

- 支持读取保持寄存器(功能码03)
- 支持读取输入寄存器(功能码04)
- 自动重连机制

### MELSEC A1E

- 三菱A系列PLC协议
- 批量读取字数据

### MQTT

- 支持订阅主题
- JSON消息解析
- QoS级别支持

## WebSocket实时通信

```
前端 ──────────────────────────────────────────────────► Django
       useWebSocket Hook                                  │
                                                          ▼
                                                   consumers.py
                                                          │
       ◄──────────────────────────────────────────────────
              实时数据推送 (采集状态、设备数据)
```

## 环境变量

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| REDIS_HOST | Redis主机 | localhost |
| REDIS_PORT | Redis端口 | 6379 |
| INFLUXDB_HOST | InfluxDB主机 | localhost |
| INFLUXDB_PORT | InfluxDB端口 | 8086 |
| INFLUXDB_TOKEN | InfluxDB认证令牌 | - |
| INFLUXDB_ORG | InfluxDB组织 | edge-iot |
| INFLUXDB_BUCKET | InfluxDB存储桶 | iot-data |
| ACQUISITION_BATCH_SIZE | 批量大小 | 50 |
| ACQUISITION_BATCH_TIMEOUT | 批量超时(秒) | 5.0 |
