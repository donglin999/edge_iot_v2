# 部署指南

> **沙永健工厂分支 (`feature/shayongjian-factory-scpi`)**：根 `docker-compose.yml`
> 采用 **host 网络 + privileged** 运行方式，与下文默认 bridge 模式不同。新增 SCPI
> 串口协议 (LINO 安规测试仪) 需直连宿主机串口与网络栈，详见下一节，部署前必读。

## 沙永健工厂：host 网络 + privileged 运行方式

本工厂分支新增 **SCPI 串口协议**（LINO 安规测试仪，走 RS-232 / USB-serial）。
按 board 硬性要求（XIU-141 / XIU-144），根 `docker-compose.yml` 的容器需要：

- **`network_mode: host`** —— 容器直接使用宿主机网络栈（采集端可直连现场设备网络）。
- **`privileged: true`** —— 后端容器授予全部容器权限，覆盖宿主机串口设备访问。

### 串口设备要求

后端三个服务（`django` / `celery-acq` / `celery-short`）都可能打开串口：

| 服务 | 何处开串口 |
| --- | --- |
| `django`（web 进程） | 设备「测试连接」接口同步调用 `SCPIProtocol.connect()` |
| `celery-acq`（`acquisition` 队列） | `start/stop_acquisition_task` 长跑采集 |
| `celery-short`（`short` 队列） | `acquire_once` / `check_protocol_connection` 一次性读/测连 |

三者均设 `privileged: true` 并显式 `devices:` 直通串口设备。设备号通过环境变量
`SCPI_SERIAL_DEVICE` 覆盖，**默认 `/dev/ttyUSB0`**：

```bash
# 部署前确认实际串口设备号（USB-serial 常见为 /dev/ttyUSB0 或 /dev/ttyACM0），
# 推荐用稳定的 by-id 路径避免插拔后改号：
ls -l /dev/serial/by-id/

# 用实际设备号启动（也可写进 .env）：
SCPI_SERIAL_DEVICE=/dev/ttyUSB0 docker compose up -d
```

> `privileged: true` 已使容器可见宿主机全部 `/dev`，显式 `devices:` 仅用于明确意图/审计；
> 若默认 `/dev/ttyUSB0` 在宿主机不存在，`docker compose up` 会因该映射失败，需先设
> `SCPI_SERIAL_DEVICE` 指向真实设备。在配置好真机的设备上 `loopback` 仿真口不需要串口。

### 与默认 bridge 模式的差异（host 网络的副作用）

host 网络与 `ports:` 端口映射**互斥**、与自定义 `networks:` 桥接**不兼容**，故本分支
compose 相对默认 bridge 模式做了如下改动：

| 项 | 默认 bridge 模式 | 本分支 host 模式 |
| --- | --- | --- |
| 网络 | 自定义桥接 `iot-network` | `network_mode: host`（顶层 `networks:` 已整体移除） |
| 端口暴露 | 各服务 `ports:` 映射 | 全部移除，进程**直接绑定宿主机端口** |
| 服务间寻址 | compose 服务名 DNS（`redis-iot` / `influxdb-iot` / `django`） | 无服务名 DNS，一律 `127.0.0.1:<port>` |

各服务在宿主机上直接监听的端口（无需 `ports:` 映射）：

| 服务 | 宿主机端口 |
| --- | --- |
| redis | 6379 |
| influxdb | 8086 |
| django | 8000 |
| frontend (Vite dev) | 5173 |
| mock-modbus | 5020 |
| mock-mqtt | 1883 / 9001 |

host 网络下「不再有 compose 服务名 DNS」，受影响的内部连接串改写如下：

- 后端 → Redis / InfluxDB：`REDIS_HOST` / `INFLUXDB_HOST` 由服务名改为 `127.0.0.1`
  （Celery broker `redis://127.0.0.1:6379/0` 由 `settings.py` 据此自动拼装）。
- 前端 Vite dev server 代理：`VITE_PROXY_TARGET` 由默认 `http://django:8000` 改为
  `http://127.0.0.1:8000`（`/api`、`/ws` 反代到本机 Django）。
- 生产以 nginx 反代静态包时，`proxy_pass` 同理需指向 `http://127.0.0.1:8000`
  （而非默认的 `http://django:8000`）。

> 端口直绑宿主机意味着这些端口**对外可达**（不再受 bridge 隔离 / `127.0.0.1:` 前缀约束）。
> 工厂内网部署可接受；若上公网，请在宿主机防火墙上收敛 6379/8086/8000 等端口。

### 校验

本分支只改部署配置、不动协议代码。提交前用静态校验确认（**勿在 1.7GiB 生产 VM 上 build/run**）：

```bash
docker compose config        # 语法 + host 网络/privileged/devices 生效校验，exit 0 即通过
```

## Docker Compose部署

### 生产环境配置

创建 `docker-compose.prod.yml`:

```yaml
version: '3.8'

services:
  redis:
    image: redis:7-alpine
    container_name: redis-iot
    ports:
      - "127.0.0.1:6379:6379"
    networks:
      - iot-network
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 3s
      retries: 3

  influxdb:
    image: influxdb:2.7
    container_name: influxdb-iot
    ports:
      - "127.0.0.1:8086:8086"
    volumes:
      - influxdb-data:/var/lib/influxdb2
      - influxdb-config:/etc/influxdb2
    environment:
      - DOCKER_INFLUXDB_INIT_MODE=setup
      - DOCKER_INFLUXDB_INIT_USERNAME=${INFLUXDB_USER}
      - DOCKER_INFLUXDB_INIT_PASSWORD=${INFLUXDB_PASSWORD}
      - DOCKER_INFLUXDB_INIT_ORG=${INFLUXDB_ORG}
      - DOCKER_INFLUXDB_INIT_BUCKET=${INFLUXDB_BUCKET}
      - DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=${INFLUXDB_TOKEN}
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "influx", "ping"]
      interval: 10s
      timeout: 3s
      retries: 5

  django:
    build:
      context: ./backend
      dockerfile: Dockerfile
    container_name: django-iot
    ports:
      - "127.0.0.1:8000:8000"
    networks:
      - iot-network
    volumes:
      - ./backend:/app
    environment:
      - DJANGO_SETTINGS_MODULE=control_plane.settings
      - REDIS_HOST=redis-iot
      - REDIS_PORT=6379
      - INFLUXDB_HOST=influxdb-iot
      - INFLUXDB_PORT=8086
      - INFLUXDB_TOKEN=${INFLUXDB_TOKEN}
      - INFLUXDB_ORG=${INFLUXDB_ORG}
      - INFLUXDB_BUCKET=${INFLUXDB_BUCKET}
      - SECRET_KEY=${DJANGO_SECRET_KEY}
      - DEBUG=False
    depends_on:
      redis:
        condition: service_healthy
      influxdb:
        condition: service_healthy
    restart: unless-stopped
    command: gunicorn control_plane.wsgi:application --bind 0.0.0.0:8000 --workers 4

  celery:
    build:
      context: ./backend
      dockerfile: Dockerfile
    container_name: celery-iot
    networks:
      - iot-network
    volumes:
      - ./backend:/app
    environment:
      - DJANGO_SETTINGS_MODULE=control_plane.settings
      - REDIS_HOST=redis-iot
      - REDIS_PORT=6379
      - INFLUXDB_HOST=influxdb-iot
      - INFLUXDB_PORT=8086
      - INFLUXDB_TOKEN=${INFLUXDB_TOKEN}
      - INFLUXDB_ORG=${INFLUXDB_ORG}
      - INFLUXDB_BUCKET=${INFLUXDB_BUCKET}
    depends_on:
      redis:
        condition: service_healthy
      influxdb:
        condition: service_healthy
    restart: unless-stopped
    command: celery -A control_plane worker -l info --pool=prefork --concurrency=4

networks:
  iot-network:
    driver: bridge

volumes:
  influxdb-data:
  influxdb-config:
```

### 环境变量

创建 `.env` 文件:

```bash
# Django
DJANGO_SECRET_KEY=your-secret-key-here

# InfluxDB
INFLUXDB_USER=admin
INFLUXDB_PASSWORD=your-secure-password
INFLUXDB_TOKEN=your-secure-token
INFLUXDB_ORG=edge-iot
INFLUXDB_BUCKET=iot-data
```

### 启动服务

```bash
# 构建并启动
docker-compose -f docker-compose.prod.yml up -d --build

# 查看日志
docker-compose -f docker-compose.prod.yml logs -f

# 检查服务状态
docker-compose -f docker-compose.prod.yml ps
```

### 初始化数据库

```bash
# 执行迁移
docker-compose -f docker-compose.prod.yml exec django python manage.py migrate

# 创建超级用户
docker-compose -f docker-compose.prod.yml exec django python manage.py createsuperuser
```

## Nginx反向代理配置

创建 `nginx.conf`:

```nginx
upstream django_iot {
    server 127.0.0.1:8000;
}

upstream frontend_iot {
    server 127.0.0.1:5173;
}

server {
    listen 80;
    server_name your-domain.com;

    # 前端静态文件
    location / {
        proxy_pass http://frontend_iot;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # API请求
    location /api/ {
        proxy_pass http://django_iot;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # WebSocket支持
    location /ws/ {
        proxy_pass http://django_iot;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

## 健康检查

### Django健康检查

```bash
curl http://localhost:8000/api/
```

### InfluxDB健康检查

```bash
curl http://localhost:8086/health
```

### Redis健康检查

```bash
redis-cli ping
```

## 日志管理

### 查看Django日志

```bash
docker-compose logs django
```

### 查看Celery日志

```bash
docker-compose logs celery
```

### 日志轮转配置

在 `docker-compose.prod.yml` 中配置日志:

```yaml
django:
  logging:
    driver: "json-file"
    options:
      max-size: "10m"
      max-file: "3"
```

## 数据备份

### InfluxDB数据备份

```bash
docker exec influxdb-iot influx backup /tmp/influxdb-backup
docker cp influxdb-iot:/tmp/influxdb-backup ./backups/
```

### SQLite数据库备份

```bash
docker-compose exec django cp /app/db.sqlite3 /tmp/db.sqlite3
docker cp django-iot:/tmp/db.sqlite3 ./backups/
```

## 故障排查

### InfluxDB认证失败

如果遇到 `401 Unauthorized` 错误:

1. 检查 `INFLUXDB_TOKEN` 环境变量配置
2. 确认 InfluxDB 容器运行正常
3. 验证 bucket 和 org 配置正确

### 设备连接超时

检查:
- 设备IP地址和端口配置
- 网络连通性
- 防火墙设置
- 设备是否在线

### Celery任务不执行

检查:
- Redis服务是否运行
- Celery Worker是否启动
- 查看Celery日志

### 前端无法连接后端

检查:
- Django服务是否运行在 `0.0.0.0:8000`
- CORS配置是否正确
- 浏览器控制台错误信息
