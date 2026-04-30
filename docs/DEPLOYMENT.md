# 部署指南

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
