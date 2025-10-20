# 快速开始指南

## 一键启动系统

```bash
./start_services.sh
```

## 常用命令

```bash
# 启动所有服务
./start_services.sh start

# 停止所有服务
./start_services.sh stop

# 重启所有服务
./start_services.sh restart

# 查看服务状态
./start_services.sh status

# 查看日志
./start_services.sh logs django   # Django 后端日志
./start_services.sh logs celery   # Celery Worker 日志
./start_services.sh logs frontend # 前端日志
```

## 访问地址

启动成功后：

| 服务 | 地址 |
|------|------|
| 前端界面 | http://localhost:5173 |
| 后端 API | http://localhost:8000 |
| API 文档 | http://localhost:8000/api/schema/swagger-ui/ |

## Mock 设备测试

启动 Mock Modbus 服务器用于测试：

```bash
# 启动两个 Mock 设备
cd mock
python3 modbus_mock_server.py --port 5020 &
python3 modbus_mock_server.py --port 5021 &

# 返回项目根目录
cd ..

# 在浏览器中访问前端，启动采集任务
```

详细说明见 [mock/USAGE.md](mock/USAGE.md)

## 故障排查

### 端口被占用

```bash
# 检查端口
lsof -i :8000  # Django
lsof -i :5173  # 前端
lsof -i :6379  # Redis

# 完全清理并重启
./start_services.sh stop
sleep 3
./start_services.sh start
```

### 服务启动失败

```bash
# 查看错误日志
./start_services.sh logs django
./start_services.sh logs celery
./start_services.sh logs frontend
```

### Redis 未运行

```bash
# 启动 Redis Docker 容器
docker start redis-test

# 或创建新容器
docker run -d --name redis -p 6379:6379 redis:latest
```

## 文件结构

```
edge_iot_v2/
├── start_services.sh          # 服务管理脚本 ⭐
├── QUICK_START.md             # 快速开始（本文件）
├── START_SERVICES_GUIDE.md    # 详细使用指南
│
├── backend/                   # Django 后端
│   ├── manage.py
│   ├── control_plane/         # 主项目
│   ├── acquisition/           # 采集模块
│   ├── configuration/         # 配置模块
│   └── ...
│
├── frontend/                  # React 前端
│   ├── package.json
│   ├── src/
│   └── ...
│
├── mock/                      # Mock 设备模拟器
│   ├── modbus_mock_server.py  # Mock Modbus 服务器
│   ├── start_mock_modbus.sh   # 启动脚本
│   ├── README.md              # 功能说明
│   ├── USAGE.md               # 使用指南
│   └── README_ARCHITECTURE.md # 架构说明
│
├── logs/                      # 服务日志（自动创建）
│   ├── django.log
│   ├── celery.log
│   └── frontend.log
│
└── .pids/                     # PID 文件（自动创建）
    ├── django.pid
    ├── celery.pid
    └── frontend.pid
```

## 开发工作流

```bash
# 1. 首次启动
./start_services.sh start

# 2. 启动 Mock 设备（可选）
cd mock && python3 modbus_mock_server.py --port 5020 & && cd ..

# 3. 打开浏览器访问 http://localhost:5173

# 4. 修改代码后重启服务
./start_services.sh restart

# 5. 完成开发后停止服务
./start_services.sh stop
```

## 更多信息

- **详细服务管理指南**: [START_SERVICES_GUIDE.md](START_SERVICES_GUIDE.md)
- **Mock 设备使用**: [mock/USAGE.md](mock/USAGE.md)
- **Mock 架构说明**: [mock/README_ARCHITECTURE.md](mock/README_ARCHITECTURE.md)
- **系统架构文档**: [docs/m2/](docs/m2/)
