# Mock Modbus 设备使用指南

## 快速开始

### 1. 启动 Mock Modbus 服务器

已经启动了两个 Mock 服务器实例：

```bash
# 查看运行中的 Mock 服务器
ps aux | grep modbus_mock_server

# 如果没有运行，启动它们：
cd /mnt/c/Users/dongl/Downloads/0907/0907/edge_iot_v2/mock

# 启动第一个 Mock 设备（端口 5020）
python3 modbus_mock_server.py --port 5020 --debug &

# 启动第二个 Mock 设备（端口 5021）
python3 modbus_mock_server.py --port 5021 --debug &
```

当前运行状态：
- Mock 服务器 1: 127.0.0.1:5020 ✅ 运行中
- Mock 服务器 2: 127.0.0.1:5021 ✅ 运行中

### 2. 设备和任务配置

系统中已经配置了以下 127.0.0.1 设备和任务：

| 设备 ID | IP | 端口 | 任务 ID | 任务名称 | 测点数量 |
|---------|------------|------|---------|----------|---------|
| 4 | 127.0.0.1 | 5020 | 4 | 海天注塑机 | 19 |
| 5 | 127.0.0.1 | 5021 | 5 | 海天注塑机1 | 19 |

### 3. 启动采集任务

#### 方式一：通过 API

```bash
# 启动任务 4（连接 127.0.0.1:5020）
curl -X POST "http://localhost:8000/api/acquisition/sessions/start-task/" \
  -H "Content-Type: application/json" \
  -d '{"task_id": 4}'

# 启动任务 5（连接 127.0.0.1:5021）
curl -X POST "http://localhost:8000/api/acquisition/sessions/start-task/" \
  -H "Content-Type: application/json" \
  -d '{"task_id": 5}'
```

#### 方式二：通过前端页面

1. 打开浏览器访问：http://localhost:5173
2. 进入"采集控制"页面
3. 找到任务"海天注塑机"或"海天注塑机1"
4. 点击"启动"按钮

### 4. 查看采集状态

```bash
# 查看活动的采集会话
curl -s http://localhost:8000/api/acquisition/sessions/active/ | python3 -m json.tool

# 查看特定会话的状态（假设会话 ID 为 14）
curl -s http://localhost:8000/api/acquisition/sessions/14/status/ | python3 -m json.tool
```

### 5. 验证数据写入 InfluxDB

```bash
# 查询 InfluxDB 中最近的数据
docker exec influxdb influx query 'from(bucket:"iot-data") |> range(start: -5m) |> limit(n: 10)' --raw
```

### 6. 在前端查看数据可视化

1. 打开浏览器访问：http://localhost:5173/data
2. 在"选择测点"下拉框中选择测点代码（例如：Device_Status）
3. 选择时间范围（最近1小时、6小时、24小时）
4. 查看实时趋势图和历史数据

## 停止采集任务

```bash
# 停止特定会话（假设会话 ID 为 14）
curl -X POST "http://localhost:8000/api/acquisition/sessions/14/stop/"

# 或通过前端页面点击"停止"按钮
```

## 停止 Mock 服务器

```bash
# 查找并停止所有 Mock 服务器进程
pkill -f modbus_mock_server
```

## 故障排查

### 问题：采集任务启动后没有数据

检查清单：

1. **Mock 服务器是否运行？**
   ```bash
   ps aux | grep modbus_mock_server
   # 应该看到两个进程在运行
   ```

2. **设备端口配置是否正确？**
   ```bash
   curl -s http://localhost:8000/api/config/devices/ | grep -A 3 "127.0.0.1"
   # Device 4 应该是端口 5020，Device 5 应该是端口 5021
   ```

3. **Celery 是否运行？**
   ```bash
   ps aux | grep celery
   # 应该看到 celery worker 进程
   ```

4. **InfluxDB 是否运行？**
   ```bash
   docker ps | grep influxdb
   # 应该看到 influxdb 容器在运行
   ```

5. **检查 Celery 日志**
   ```bash
   # 在后台服务的输出中应该能看到类似的日志：
   # "Connected to Modbus TCP device 127.0.0.1:5020"
   # "Writing 19 points to storage"
   ```

### 问题：前端看不到数据

1. **检查是否有数据写入 InfluxDB**
   ```bash
   docker exec influxdb influx query 'from(bucket:"iot-data") |> range(start: -1h) |> limit(n: 10)' --raw
   ```

2. **检查 API 是否返回数据**
   ```bash
   curl -s "http://localhost:8000/api/acquisition/sessions/point-history/?point_code=Device_Status&start_time=-1h&limit=10"
   ```

3. **检查浏览器控制台是否有错误**
   - 按 F12 打开开发者工具
   - 查看 Console 和 Network 标签页

## Mock 数据生成规则

Mock 服务器根据寄存器地址生成随机值：

```python
base_value = (地址 % 100) * 100
random_value = base_value + random(0, 1000)
```

示例：
- 地址 0 → 返回 0-1000
- 地址 1 → 返回 100-1100
- 地址 50 → 返回 5000-6000

每次读取都会生成新的随机值，模拟真实设备的数据变化。
