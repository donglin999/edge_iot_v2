# M1 到 M2 迁移指南

## 快速开始

本指南帮助你从 M1 的双系统架构平滑过渡到 M2 的统一 Django 架构。

---

## 环境准备

### 1. 依赖安装

更新 `requirements.txt` (如果尚未更新):

```bash
# 确保已有以下包
pip install django djangorestframework drf-spectacular
pip install celery redis
pip install influxdb-client kafka-python
pip install modbus-tk paho-mqtt
```

### 2. 数据库迁移

```bash
cd backend

# 创建迁移文件
python manage.py makemigrations acquisition

# 应用迁移
python manage.py migrate
```

**预期输出:**

```
Migrations for 'acquisition':
  acquisition/migrations/0001_initial.py
    - Create model AcquisitionSession
    - Create model DataPoint
    - Add index...

Running migrations:
  Applying acquisition.0001_initial... OK
```

### 3. 配置文件

在 `backend/.env` 创建或更新:

```ini
# Django
DEBUG=True
SECRET_KEY=your-secret-key-here

# Database (默认SQLite,生产环境建议PostgreSQL)
DJANGO_DB_NAME=/path/to/backend/db.sqlite3

# Celery & Redis
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/0
CELERY_TASK_ALWAYS_EAGER=False  # 生产环境设为False

# InfluxDB
INFLUXDB_HOST=localhost
INFLUXDB_PORT=8086
INFLUXDB_TOKEN=your-influxdb-token
INFLUXDB_ORG=your-org
INFLUXDB_BUCKET=your-bucket

# Kafka (可选)
KAFKA_ENABLED=False
KAFKA_BOOTSTRAP_SERVERS=localhost:9092
KAFKA_TOPIC=acquisition_data
```

---

## 启动服务

### 方式一: 分步启动 (推荐调试)

**终端1 - Django服务:**

```bash
cd backend
python manage.py runserver 0.0.0.0:8000
```

**终端2 - Celery Worker:**

```bash
cd backend
celery -A control_plane worker -l info --pool=solo  # Windows使用solo
# Linux/Mac: celery -A control_plane worker -l info
```

**终端3 - Celery Beat (定时任务,可选):**

```bash
celery -A control_plane beat -l info
```

### 方式二: Docker Compose (推荐生产)

创建 `docker-compose.yml`:

```yaml
version: '3.8'

services:
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"

  postgres:
    image: postgres:15-alpine
    environment:
      POSTGRES_DB: edge_iot
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data

  influxdb:
    image: influxdb:2.7
    ports:
      - "8086:8086"
    environment:
      DOCKER_INFLUXDB_INIT_MODE: setup
      DOCKER_INFLUXDB_INIT_USERNAME: admin
      DOCKER_INFLUXDB_INIT_PASSWORD: adminadmin
      DOCKER_INFLUXDB_INIT_ORG: edge_iot
      DOCKER_INFLUXDB_INIT_BUCKET: default
    volumes:
      - influxdb_data:/var/lib/influxdb2

  backend:
    build: ./backend
    command: python manage.py runserver 0.0.0.0:8000
    ports:
      - "8000:8000"
    depends_on:
      - redis
      - postgres
      - influxdb
    env_file:
      - backend/.env

  celery:
    build: ./backend
    command: celery -A control_plane worker -l info
    depends_on:
      - redis
      - postgres
    env_file:
      - backend/.env

volumes:
  postgres_data:
  influxdb_data:
```

启动:

```bash
docker-compose up -d
```

---

## 迁移现有配置

### 从 Excel 导入配置

#### 步骤1: 上传 Excel

```bash
curl -X POST http://localhost:8000/api/config/import-jobs/ \
  -F "file=@数据地址清单.xlsx" \
  -F "triggered_by=admin"
```

**响应示例:**

```json
{
  "id": 1,
  "source_name": "数据地址清单.xlsx",
  "status": "validated",
  "summary": {
    "rows_parsed": 150,
    "connection_count": 3,
    "device_tag_count": 10,
    "created_points": 150,
    "metadata": {
      "protocols": "modbustcp,plc"
    }
  }
}
```

#### 步骤2: 应用配置

```bash
curl -X POST http://localhost:8000/api/config/import-jobs/1/apply/ \
  -H "Content-Type: application/json" \
  -d '{"site_code": "factory_01", "created_by": "admin"}'
```

### 从旧系统迁移

如果你之前使用 `run.py` 启动采集,现在需要:

1. **停止旧系统:**

```bash
# 找到旧进程
ps aux | grep "run.py"
# 终止进程
kill <PID>
```

2. **验证配置已导入:**

```bash
curl http://localhost:8000/api/config/tasks/
```

3. **启动新系统采集:**

```bash
# 启动任务ID为1的采集
curl -X POST http://localhost:8000/api/config/tasks/1/start/ \
  -H "Content-Type: application/json" \
  -d '{"worker": "worker-001", "note": "从M1迁移"}'
```

---

## 验证功能

### 1. 测试协议连接

```python
# Python 交互式测试
from acquisition.protocols import ProtocolRegistry

config = {
    "source_ip": "192.168.1.100",
    "source_port": 502,
    "protocol_type": "modbustcp"
}

protocol = ProtocolRegistry.create("modbustcp", config)
with protocol:
    print(f"连接状态: {protocol.is_connected}")
    print(f"健康检查: {protocol.health_check()}")
```

### 2. 测试存储连接

```python
from storage import StorageRegistry

influx_config = {
    "host": "localhost",
    "port": 8086,
    "token": "your-token",
    "org": "your-org",
    "bucket": "your-bucket"
}

storage = StorageRegistry.create("influxdb", influx_config)
with storage:
    test_data = [{
        "measurement": "test",
        "tags": {"site": "test"},
        "fields": {"value": 123}
    }]
    print(f"写入成功: {storage.write(test_data)}")
```

### 3. 单次采集测试

```bash
# 通过API触发单次采集
curl -X POST http://localhost:8000/api/acquisition/acquire-once/ \
  -H "Content-Type: application/json" \
  -d '{"task_id": 1}'
```

### 4. 查看采集会话

```bash
curl http://localhost:8000/api/acquisition/sessions/
```

---

## 常见问题

### Q1: Celery worker 无法启动

**错误:** `kombu.exceptions.OperationalError: [Errno 111] Connection refused`

**解决:**

```bash
# 检查 Redis 是否运行
redis-cli ping
# 应该返回 PONG

# 如果未运行,启动 Redis
redis-server
```

### Q2: InfluxDB 连接失败

**错误:** `influxdb_client.rest.ApiException: (401) Unauthorized`

**解决:**

1. 检查 token 是否正确
2. 在 InfluxDB UI 生成新 token

```bash
# 打开 InfluxDB UI
open http://localhost:8086

# Data > API Tokens > Generate > All Access API Token
```

### Q3: 协议库导入错误

**错误:** `ModuleNotFoundError: No module named 'modbus_tk'`

**解决:**

```bash
pip install modbus-tk

# 或者针对 PLC
pip install HslCommunication  # 如果使用Mitsubishi PLC
```

### Q4: 采集任务无法停止

**问题:** 调用 `/tasks/{id}/stop/` 后任务仍在运行

**解决:**

```bash
# 方法1: 通过API停止会话
curl -X POST http://localhost:8000/api/acquisition/sessions/{session_id}/stop/

# 方法2: 重启 Celery worker
celery -A control_plane control shutdown
celery -A control_plane worker -l info
```

### Q5: 数据未写入 InfluxDB

**检查清单:**

1. InfluxDB 是否运行?

```bash
curl http://localhost:8086/health
```

2. Bucket 是否存在?

```bash
influx bucket list --org your-org
```

3. 查看采集日志

```bash
tail -f backend/logs/acquisition.log
```

---

## 性能调优

### 1. Celery 并发数

根据 CPU 核心数调整:

```bash
# 4核CPU,启动8个worker
celery -A control_plane worker -l info --concurrency=8
```

### 2. InfluxDB 批量写入

在 `acquisition_service.py` 调整:

```python
batch_size = 100  # 默认50,可增加到100-500
```

### 3. 连接池优化

对于频繁连接的设备,使用连接池:

```python
# 在 protocol 中保持长连接
protocol.keep_alive = True
```

---

## 回滚方案

如果遇到严重问题需要回滚:

### 1. 恢复旧系统

```bash
# 停止新系统
docker-compose down  # 或手动停止服务

# 启动旧系统
cd /path/to/edge_iot_v2
python run.py
```

### 2. 数据恢复

```bash
# 备份数据库
cp backend/db.sqlite3 backend/db.sqlite3.backup

# 恢复旧版本数据库
cp backend/db.sqlite3.m1 backend/db.sqlite3
python manage.py migrate configuration 0003  # 回滚到M1迁移
```

---

## 下一步

✅ **完成迁移后:**

1. 监控采集任务运行状态
2. 检查 InfluxDB 数据完整性
3. 对比新旧系统性能指标
4. 逐步废弃 `apps/` 目录代码
5. 编写自动化测试

📚 **相关文档:**

- [后端重构文档](./backend_refactoring.md)
- [API 文档](http://localhost:8000/api/docs/)
- [Celery 最佳实践](https://docs.celeryproject.org/en/stable/userguide/tasks.html)

---

**需要帮助?** 查看 `backend/logs/` 日志或联系开发团队。
