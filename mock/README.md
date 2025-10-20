# Mock Modbus 设备模拟器

用于模拟 Modbus TCP 设备，返回随机数据供系统测试使用。

## 功能特性

- ✅ 支持 Modbus TCP 协议
- ✅ 支持读保持寄存器 (0x03)
- ✅ 支持读输入寄存器 (0x04)
- ✅ 支持读线圈 (0x01)
- ✅ 支持读离散输入 (0x02)
- ✅ 返回基于地址的随机数据
- ✅ 详细的日志记录

## 快速开始

### 1. 使用脚本启动（推荐）

```bash
cd mock
chmod +x start_mock_modbus.sh
./start_mock_modbus.sh
```

脚本会自动：
- 检测端口占用情况
- 如果 502 端口被占用，自动使用 5020 端口
- 如果使用 502 端口，会提示输入 sudo 密码

### 2. 手动启动

#### 使用默认配置（127.0.0.1:502）

```bash
cd mock
sudo python3 modbus_mock_server.py
```

**注意**: 端口 502 需要 root 权限

#### 使用自定义端口（无需 sudo）

```bash
cd mock
python3 modbus_mock_server.py --host 127.0.0.1 --port 5020
```

#### 启用调试日志

```bash
python3 modbus_mock_server.py --port 5020 --debug
```

## 命令行参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--host` | 127.0.0.1 | 绑定的主机地址 |
| `--port` | 502 | 绑定的端口号 |
| `--debug` | False | 启用调试日志 |

## 数据生成规则

### 寄存器值（保持寄存器/输入寄存器）

```python
base_value = (地址 % 100) * 100
random_value = base_value + random(0, 1000)
```

示例：
- 地址 0: 0 + random(0, 1000) = 0-1000
- 地址 1: 100 + random(0, 1000) = 100-1100
- 地址 50: 5000 + random(0, 1000) = 5000-6000
- 地址 100: 0 + random(0, 1000) = 0-1000

### 线圈/离散输入

返回随机布尔值（0 或 1）

## 配置系统连接 Mock 设备

### 修改设备配置中的端口号

如果 Mock 服务器运行在非 502 端口（例如 5020），需要在系统配置中修改：

1. **通过 Excel 导入**：
   - 在 Excel 配置表的 `source_port` 列填写 `5020`

2. **通过 API 修改**：
   ```bash
   curl -X PATCH http://localhost:8000/api/config/devices/{device_id}/ \
     -H "Content-Type: application/json" \
     -d '{"source_port": 5020}'
   ```

3. **通过数据库直接修改**：
   ```sql
   UPDATE configuration_device
   SET source_port = 5020
   WHERE source_ip = '127.0.0.1';
   ```

### 启动采集任务

```bash
# 查看可用的采集任务
curl http://localhost:8000/api/config/tasks/

# 启动指定任务（假设任务 ID 为 4）
curl -X POST http://localhost:8000/api/acquisition/sessions/start-task/ \
  -H "Content-Type: application/json" \
  -d '{"task_id": 4}'
```

## 测试 Mock 服务器

### 使用 Python 测试

```python
from pymodbus.client import ModbusTcpClient

client = ModbusTcpClient('127.0.0.1', port=5020)
client.connect()

# 读取 10 个保持寄存器，从地址 0 开始
result = client.read_holding_registers(0, 10, unit=1)
print(f"寄存器值: {result.registers}")

client.close()
```

### 使用 modpoll 工具测试

```bash
# 读取保持寄存器
modpoll -m tcp -a 1 -r 0 -c 10 -t 4 127.0.0.1 -p 5020

# 参数说明:
# -m tcp: 使用 Modbus TCP
# -a 1: 从站地址 1
# -r 0: 起始地址 0
# -c 10: 读取 10 个寄存器
# -t 4: 寄存器类型（4=保持寄存器）
# -p 5020: 端口号
```

## 日志输出示例

```
2025-10-10 19:30:00 - ModbusMockServer - INFO - Mock Modbus TCP Server started on 127.0.0.1:5020
2025-10-10 19:30:00 - ModbusMockServer - INFO - Waiting for connections...
2025-10-10 19:30:15 - ModbusMockServer - INFO - New connection from ('127.0.0.1', 54321)
2025-10-10 19:30:15 - ModbusMockServer - INFO - Read Holding Registers - Start: 0, Quantity: 10
2025-10-10 19:30:15 - ModbusMockServer - DEBUG - Response - Registers: [234, 1045, 2678, 3123, ...]
```

## 故障排查

### 1. 端口占用

```bash
# 检查端口占用
lsof -i :502
lsof -i :5020

# 强制使用其他端口
python3 modbus_mock_server.py --port 6020
```

### 2. 权限错误

如果遇到 `Permission denied`，说明端口 < 1024 需要 root 权限：

```bash
sudo python3 modbus_mock_server.py --port 502
```

或使用端口 >= 1024：

```bash
python3 modbus_mock_server.py --port 5020
```

### 3. 连接被拒绝

- 检查防火墙设置
- 确认服务器正在运行
- 检查客户端连接的 IP 和端口是否正确

## 停止服务器

按 `Ctrl+C` 停止服务器，或发送 SIGINT 信号：

```bash
pkill -f modbus_mock_server
```

## 多设备模拟

可以启动多个 Mock 服务器实例，模拟多个设备：

```bash
# 设备 1 - 端口 5020
python3 modbus_mock_server.py --port 5020 &

# 设备 2 - 端口 5021
python3 modbus_mock_server.py --port 5021 &

# 设备 3 - 端口 5022
python3 modbus_mock_server.py --port 5022 &
```

## 技术细节

### Modbus TCP 帧结构

```
MBAP Header (7 bytes):
  - Transaction ID (2 bytes)
  - Protocol ID (2 bytes) - 固定为 0x0000
  - Length (2 bytes) - 后续字节数
  - Unit ID (1 byte) - 从站地址

PDU:
  - Function Code (1 byte)
  - Data (N bytes)
```

### 支持的功能码

| 功能码 | 名称 | 说明 |
|--------|------|------|
| 0x01 | Read Coils | 读线圈 |
| 0x02 | Read Discrete Inputs | 读离散输入 |
| 0x03 | Read Holding Registers | 读保持寄存器 |
| 0x04 | Read Input Registers | 读输入寄存器 |
