# 快速开始

## 环境要求

- Python 3.10+
- Node.js 18+
- Docker & Docker Compose
- Redis 7+

## 一、启动基础服务

```bash
# 启动Docker服务（Redis, InfluxDB）
docker-compose up -d

# 检查服务状态
docker ps
```

## 二、后端服务

```bash
cd backend

# 安装依赖
pip install -r requirements.txt

# 数据库迁移
python3 manage.py migrate

# 启动Django服务
python3 manage.py runserver 0.0.0.0:8000
```

## 三、Celery Worker

```bash
cd backend

# 启动Celery Worker
celery -A control_plane worker -l info --pool=solo
```

## 四、前端服务

```bash
cd frontend

# 安装依赖（首次）
npm install

# 启动开发服务器
npm run dev
```

## 五、一键启动（推荐）

```bash
# 启动所有服务
./start_services.sh

# 访问前端
# http://localhost:5173

# 访问后端API
# http://localhost:8000/api/
```

## 验证安装

### 检查后端服务

```bash
curl http://localhost:8000/api/
```

### 检查活动会话

```bash
curl http://localhost:8000/api/acquisition/sessions/active/
```

### 检查InfluxDB

```bash
curl http://localhost:8086/health
```

## 下一步

- [系统架构](SYSTEM_ARCHITECTURE.md) - 了解系统架构
- [API参考](API_REFERENCE.md) - 查看API文档
- [部署指南](DEPLOYMENT.md) - 生产环境部署
