# API参考

## 基础信息

- 基础URL: `http://localhost:8000/api/`
- 认证方式: Token认证
- 返回格式: JSON

## 采集控制 API

### 启动任务

```
POST /api/acquisition/sessions/start-task/
```

**请求体:**
```json
{
  "task_id": 1
}
```

**响应:**
```json
{
  "id": 1,
  "task": 1,
  "status": "running",
  "started_at": "2024-01-01T10:00:00Z"
}
```

### 停止任务

```
POST /api/acquisition/sessions/{id}/stop/
```

**响应:**
```json
{
  "id": 1,
  "status": "stopped",
  "stopped_at": "2024-01-01T10:30:00Z"
}
```

### 查询活动会话

```
GET /api/acquisition/sessions/active/
```

**响应:**
```json
{
  "count": 1,
  "results": [
    {
      "id": 1,
      "task": {
        "id": 1,
        "code": "task_001",
        "name": "采集任务1"
      },
      "status": "running",
      "started_at": "2024-01-01T10:00:00Z"
    }
  ]
}
```

### 查询历史数据

```
GET /api/acquisition/sessions/point-history/?point_code=temperature1&start_time=-1h&limit=100
```

**参数:**
| 参数 | 类型 | 说明 |
| --- | --- | --- |
| point_code | string | 测点编码 |
| start_time | string | 开始时间 (RFC3339或相对时间如-1h) |
| stop_time | string | 结束时间 (可选) |
| limit | int | 返回条数限制 |

**响应:**
```json
{
  "count": 100,
  "results": [
    {
      "point_code": "temperature1",
      "timestamp": "2024-01-01T10:00:00Z",
      "value": 25.5,
      "quality": "good"
    }
  ]
}
```

### 获取会话详情

```
GET /api/acquisition/sessions/{id}/
```

### 获取会话数据点

```
GET /api/acquisition/sessions/{id}/points/
```

## 配置管理 API

### Excel导入

```
POST /api/config/import/excel/
Content-Type: multipart/form-data

file: <excel-file>
```

**Excel格式:**

设备表 (devices):
| code | name | protocol_type | host | port |
| --- | --- | --- | --- | --- |
| device_001 | 设备1 | modbus_tcp | 192.168.1.100 | 502 |

测点表 (points):
| code | name | device_code | address | data_type |
| --- | --- | --- | --- | --- |
| temp_001 | 温度1 | device_001 | 0 | float |

任务表 (tasks):
| code | name | device_code | point_codes | interval |
| --- | --- | --- | --- | --- |
| task_001 | 任务1 | device_001 | temp_001 | 5 |

**响应:**
```json
{
  "import_job_id": 1,
  "status": "completed",
  "devices_created": 1,
  "points_created": 1,
  "tasks_created": 1
}
```

### 查询设备列表

```
GET /api/config/devices/
```

**响应:**
```json
{
  "count": 2,
  "results": [
    {
      "id": 1,
      "code": "device_001",
      "name": "设备1",
      "protocol_type": "modbus_tcp",
      "host": "192.168.1.100",
      "port": 502,
      "is_online": true
    }
  ]
}
```

### 查询测点列表

```
GET /api/config/points/
```

**参数:**
| 参数 | 类型 | 说明 |
| --- | --- | --- |
| device_code | string | 设备编码 (可选) |

**响应:**
```json
{
  "count": 10,
  "results": [
    {
      "id": 1,
      "code": "temp_001",
      "name": "温度1",
      "device": "device_001",
      "address": "0",
      "data_type": "float"
    }
  ]
}
```

### 查询任务列表

```
GET /api/config/tasks/
```

**响应:**
```json
{
  "count": 2,
  "results": [
    {
      "id": 1,
      "code": "task_001",
      "name": "任务1",
      "device": "device_001",
      "points": ["temp_001"],
      "interval": 5,
      "is_active": true
    }
  ]
}
```

### 获取任务详情

```
GET /api/config/tasks/{id}/
```

### 创建任务

```
POST /api/config/tasks/
```

**请求体:**
```json
{
  "code": "task_002",
  "name": "任务2",
  "device": 1,
  "points": [1, 2, 3],
  "interval": 10
}
```

### 更新任务

```
PUT /api/config/tasks/{id}/
```

### 删除任务

```
DELETE /api/config/tasks/{id}/
```

## 数据可视化 API

### 查询聚合数据

```
GET /api/data/aggregated/?measurement=sensor_data&field=value&func=mean&start_time=-1h&interval=5m
```

**参数:**
| 参数 | 类型 | 说明 |
| --- | --- | --- |
| measurement | string | 测量名称 |
| field | string | 字段名 |
| func | string | 聚合函数 (mean, max, min, sum) |
| start_time | string | 开始时间 |
| stop_time | string | 结束时间 (可选) |
| interval | string | 聚合间隔 (如5m, 1h) |

**响应:**
```json
{
  "count": 12,
  "results": [
    {
      "time": "2024-01-01T10:00:00Z",
      "value": 25.3
    }
  ]
}
```

## WebSocket API

### 连接

```
ws://localhost:8000/ws/acquisition/{session_id}/
```

### 消息格式

服务端推送:
```json
{
  "type": "data",
  "point_code": "temperature1",
  "value": 25.5,
  "timestamp": "2024-01-01T10:00:00Z",
  "quality": "good"
}
```

心跳:
```json
{
  "type": "ping"
}
```

## 错误响应

**格式:**
```json
{
  "error": "error_code",
  "message": "错误描述",
  "details": {}
}
```

**错误码:**
| 错误码 | HTTP状态 | 说明 |
| --- | --- | --- |
| VALIDATION_ERROR | 400 | 参数验证失败 |
| NOT_FOUND | 404 | 资源不存在 |
| INTERNAL_ERROR | 500 | 服务器内部错误 |
