# Mock 设备架构说明

## 架构变更

### 之前的设计（已废弃）

在系统代码中添加 `mock_mode` 检测 127.0.0.1 地址，并在协议层返回模拟数据：

```python
# ❌ 已移除
class ModbusTCPProtocol:
    def __init__(self, device_config):
        self.mock_mode = self.ip in ["127.0.0.1", "localhost"]

    def read_points(self, points):
        if self.mock_mode:
            return self._read_points_mock(points)  # 内置模拟
```

**问题**：
- 测试代码与生产代码混合
- 不符合真实设备通讯场景
- Mock 逻辑侵入系统核心代码

### 当前设计（正确）

创建独立的 Mock Modbus TCP 服务器，作为外部模拟设备运行：

```
┌─────────────────────────────────────────────────────────┐
│                     系统架构                               │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  ┌──────────────┐      TCP/IP       ┌───────────────┐  │
│  │              │ ◄─────────────────►│ Mock Modbus   │  │
│  │  Acquisition │                    │ Server        │  │
│  │  Service     │                    │ (127.0.0.1:   │  │
│  │  (系统代码)   │                    │  5020/5021)   │  │
│  │              │                    └───────────────┘  │
│  │              │      TCP/IP       ┌───────────────┐  │
│  │              │ ◄─────────────────►│ 真实 PLC      │  │
│  │              │                    │ (10.41.68.49) │  │
│  └──────────────┘                    └───────────────┘  │
│         │                                                │
│         ▼                                                │
│  ┌──────────────┐                                        │
│  │  InfluxDB    │                                        │
│  └──────────────┘                                        │
└─────────────────────────────────────────────────────────┘
```

**优点**：
- ✅ 系统代码与测试代码完全分离
- ✅ Mock 设备可独立启动/停止
- ✅ 模拟真实的网络通讯场景
- ✅ 可以同时运行多个 Mock 设备（不同端口）
- ✅ Mock 设备可以被其他系统/工具使用（如 modpoll）

## 实现细节

### 1. Mock 服务器 (modbus_mock_server.py)

完整实现 Modbus TCP 协议规范：

- **MBAP Header 解析**：Transaction ID、Protocol ID、Length、Unit ID
- **支持的功能码**：
  - 0x01: Read Coils（读线圈）
  - 0x02: Read Discrete Inputs（读离散输入）
  - 0x03: Read Holding Registers（读保持寄存器）
  - 0x04: Read Input Registers（读输入寄存器）
- **随机数据生成**：基于地址生成可预测但随机的数值
- **异常处理**：返回标准 Modbus 异常响应

### 2. 系统代码 (acquisition/protocols/modbus.py)

完全移除了 Mock 相关代码：

```python
# ✅ 干净的生产代码
class ModbusTCPProtocol(BaseProtocol):
    def __init__(self, device_config: Dict[str, Any]) -> None:
        super().__init__(device_config)
        self.ip = device_config.get("source_ip")
        self.port = device_config.get("source_port", 502)
        # ... 正常初始化，没有 mock_mode

    def connect(self) -> bool:
        """正常的 TCP 连接"""
        self.master = modbus_tcp.TcpMaster(
            host=self.ip,
            port=self.port,
            timeout_in_sec=self.timeout
        )
        return True

    def read_points(self, points):
        """正常的数据读取，无特殊分支"""
        data = self.master.execute(...)
        return results
```

### 3. 配置管理

设备配置中 127.0.0.1 地址没有任何特殊标记：

```json
{
  "id": 4,
  "code": "modbustcp-127.0.0.1-4196",
  "name": "海天注塑机",
  "protocol": "modbustcp",
  "ip_address": "127.0.0.1",
  "port": 5020
}
```

系统将其视为普通 Modbus TCP 设备，唯一区别是 IP 地址指向本机。

## 使用场景

### 开发环境

```bash
# 启动 Mock 设备模拟器
cd mock
python3 modbus_mock_server.py --port 5020 &
python3 modbus_mock_server.py --port 5021 &

# 配置系统连接 127.0.0.1:5020 和 127.0.0.1:5021
# 启动采集任务 → 系统正常采集数据 → 写入 InfluxDB
```

### 测试环境

```bash
# 运行多个 Mock 设备模拟不同场景
python3 modbus_mock_server.py --port 5020 &  # 正常设备
python3 modbus_mock_server.py --port 5021 &  # 另一个设备

# 手动测试工具
modpoll -m tcp -a 1 -r 0 -c 10 -t 4 127.0.0.1 -p 5020
```

### 生产环境

```bash
# 不启动 Mock 服务器
# 配置真实 PLC IP 地址：10.41.68.49、10.41.68.50 等
# 系统连接真实设备采集数据
```

## 文件结构

```
edge_iot_v2/
├── mock/                                  # Mock 设备目录
│   ├── modbus_mock_server.py             # Mock Modbus TCP 服务器
│   ├── start_mock_modbus.sh              # 启动脚本
│   ├── README.md                         # 使用说明
│   ├── README_ARCHITECTURE.md            # 架构说明（本文件）
│   └── USAGE.md                          # 快速使用指南
│
├── backend/
│   ├── acquisition/
│   │   └── protocols/
│   │       └── modbus.py                 # ✅ 干净的 Modbus 协议实现
│   └── ...
│
└── ...
```

## 扩展性

### 添加其他协议的 Mock 服务器

遵循相同的架构模式：

```bash
mock/
├── modbus_mock_server.py     # Modbus TCP Mock
├── opcua_mock_server.py      # OPC UA Mock（待实现）
├── mqtt_mock_broker.py       # MQTT Mock（待实现）
└── ...
```

### 添加更多 Mock 场景

```python
# 可以扩展 Mock 服务器支持不同的数据模式
class ModbusMockServer:
    def __init__(self, host, port, scenario='random'):
        self.scenario = scenario

    def generate_value(self, address):
        if self.scenario == 'random':
            return random_value()
        elif self.scenario == 'sine_wave':
            return sine_wave_value(address)
        elif self.scenario == 'static':
            return fixed_value(address)
```

## 总结

这种架构完全符合"关注点分离"和"依赖倒置"原则：

- **系统代码**：只负责实现 Modbus TCP 客户端协议
- **Mock 服务器**：独立实现 Modbus TCP 服务端协议
- **两者通过标准 TCP/IP 和 Modbus 协议通讯**
- **互不依赖，可独立开发、测试、部署**

这正是软件工程中推荐的测试替身（Test Double）模式。
