## 测试套件文档

本目录包含后端所有单元测试和集成测试。

### 📦 测试结构

```
tests/
├── conftest.py                    # Pytest配置和全局fixtures
├── pytest.ini                     # Pytest设置
├── requirements-test.txt          # 测试依赖
├── mocks/                         # Mock对象
│   ├── protocols.py              # Mock协议实现
│   └── storage.py                # Mock存储实现
├── fixtures/                      # 测试数据工厂
│   └── factories.py              # Django模型工厂函数
├── test_protocols.py              # 协议层测试
├── test_storage.py                # 存储层测试
├── test_acquisition_service.py    # 服务层测试
└── test_integration.py            # 集成测试
```

---

### 🚀 快速开始

#### 1. 安装测试依赖

```bash
cd backend
pip install -r tests/requirements-test.txt
```

#### 2. 运行所有测试

```bash
# 运行所有测试
pytest

# 详细输出
pytest -v

# 显示覆盖率
pytest --cov=acquisition --cov=storage

# 运行特定测试文件
pytest tests/test_protocols.py

# 运行特定测试类
pytest tests/test_protocols.py::TestMockModbusTCP

# 运行特定测试方法
pytest tests/test_protocols.py::TestMockModbusTCP::test_connection_success
```

---

### 🧪 测试类型

#### 单元测试

**协议层测试** (`test_protocols.py`)
- 测试协议注册机制
- 测试ModbusTCP/PLC/MQTT mock实现
- 测试连接/读取/断开流程
- 测试错误处理

**存储层测试** (`test_storage.py`)
- 测试存储注册机制
- 测试InfluxDB/Kafka mock实现
- 测试数据写入
- 测试健康检查

**服务层测试** (`test_acquisition_service.py`)
- 测试AcquisitionService初始化
- 测试数据采集流程
- 测试数据格式化
- 测试存储写入

#### 集成测试

**端到端测试** (`test_integration.py`)
- 完整采集流程: 设备→协议→服务→存储
- 多设备采集
- 混合协议采集
- 异常恢复测试

---

### 🎭 Mock对象

#### Mock协议

**MockModbusTCPProtocol**
```python
config = {
    "source_ip": "192.168.1.100",
    "source_port": 502,
    "_test_connection_fail": False,  # 模拟连接失败
    "_test_read_fail": False,        # 模拟读取失败
    "_test_simulated_data": {        # 预定义数据
        "POINT_001": 100,
        "POINT_002": 200,
    }
}
```

**MockPLCProtocol**
```python
config = {
    "_test_simulated_data": {
        "INT_POINT": 42,
        "FLOAT_POINT": 25.5,
    }
}
```

**MockMQTTProtocol**
```python
config = {
    "mqtt_topics": ["test/topic"],
    "_test_messages": [
        {
            "code": "SENSOR_001",
            "value": {"temp": 25.0},
            "timestamp": 1234567890000000000,
            "quality": "good",
        }
    ]
}
```

#### Mock存储

**MockInfluxDBStorage**
```python
config = {
    "_test_connect_fail": False,  # 模拟连接失败
    "_test_write_fail": False,    # 模拟写入失败
}

# 获取写入的数据
storage.get_written_data()

# 清空数据
storage.clear_data()
```

**MockKafkaStorage**
```python
# 获取发送的消息
storage.get_sent_messages()

# 清空消息
storage.clear_messages()
```

---

### 🏭 Fixtures 使用

测试工厂函数可创建测试数据:

```python
def test_example(create_site, create_device, create_point, create_task):
    # 创建站点
    site = create_site(code="TEST", name="Test Site")

    # 创建设备
    device = create_device(
        site=site,
        protocol="mock_modbus",
        ip="192.168.1.100",
        metadata={"_test_simulated_data": {"P1": 100}}
    )

    # 创建测点
    point = create_point(
        device=device,
        code="P1",
        address="D100"
    )

    # 创建任务
    task = create_task(
        code="TEST_TASK",
        points=[point]
    )
```

---

### 📊 测试覆盖率

查看详细覆盖率报告:

```bash
# 生成HTML报告
pytest --cov=acquisition --cov=storage --cov-report=html

# 打开报告
open htmlcov/index.html  # macOS
# 或
start htmlcov/index.html  # Windows
```

目标覆盖率:
- **协议层**: ≥80%
- **存储层**: ≥80%
- **服务层**: ≥70%
- **总体**: ≥75%

---

### ✅ 测试清单

在提交PR前,确保以下测试通过:

- [ ] `test_protocols.py` - 所有协议测试
- [ ] `test_storage.py` - 所有存储测试
- [ ] `test_acquisition_service.py` - 服务层测试
- [ ] `test_integration.py` - 集成测试
- [ ] 代码覆盖率 ≥75%
- [ ] 无警告或弃用提示

---

### 🐛 调试技巧

#### 查看详细输出

```bash
pytest -vv --tb=long
```

#### 仅运行失败的测试

```bash
pytest --lf  # last-failed
```

#### 进入调试器

```python
def test_example():
    import pdb; pdb.set_trace()  # 设置断点
    # ... 测试代码
```

或使用pytest断点:

```bash
pytest --pdb  # 失败时自动进入调试器
```

#### 显示打印输出

```bash
pytest -s  # 显示print()输出
```

---

### 🔧 常见问题

**Q: 测试时找不到Django模块?**

A: 确保在backend目录运行测试,或设置PYTHONPATH:

```bash
export PYTHONPATH=/path/to/backend:$PYTHONPATH
pytest
```

**Q: 数据库错误?**

A: 测试使用内存SQLite,确保`conftest.py`正确配置:

```python
settings.DATABASES["default"] = {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": ":memory:",
}
```

**Q: Mock对象未注册?**

A: 检查`autouse=True` fixture是否正确:

```python
@pytest.fixture(autouse=True)
def setup_mocks():
    register_mock_protocols()
    register_mock_storage()
    yield
```

---

### 📝 编写新测试

#### 1. 协议测试模板

```python
class TestNewProtocol:
    def test_connection(self):
        protocol = ProtocolRegistry.create("new_protocol", config)
        assert protocol.connect()

    def test_read_points(self):
        protocol = ProtocolRegistry.create("new_protocol", config)
        protocol.connect()
        results = protocol.read_points(points)
        assert len(results) > 0
```

#### 2. 服务测试模板

```python
@pytest.mark.django_db
class TestNewService:
    @patch("module.settings")
    def test_service_function(self, mock_settings, create_task):
        mock_settings.SOME_CONFIG = "value"
        task = create_task()
        # ... 测试逻辑
```

#### 3. 集成测试模板

```python
@pytest.mark.django_db
class TestNewIntegration:
    @patch("acquisition.services.acquisition_service.StorageRegistry")
    def test_full_flow(self, mock_storage, create_device, create_point):
        # Setup
        device = create_device(protocol="mock_modbus")
        point = create_point(device=device)

        # Execute
        # ... 执行完整流程

        # Verify
        # ... 验证结果
```

---

### 🎯 最佳实践

1. **一个测试一个目的** - 每个测试只验证一个行为
2. **使用有意义的名称** - `test_connection_failure_returns_false`
3. **Arrange-Act-Assert** - 清晰的三段结构
4. **独立性** - 测试间不应有依赖
5. **快速执行** - 使用Mock避免实际I/O
6. **清理资源** - 使用fixtures自动清理

---

### 📚 参考资料

- [Pytest文档](https://docs.pytest.org/)
- [Pytest-Django](https://pytest-django.readthedocs.io/)
- [Django测试](https://docs.djangoproject.com/en/stable/topics/testing/)
- [Mock对象](https://docs.python.org/3/library/unittest.mock.html)

---

**需要帮助?** 查看现有测试代码或联系测试负责人。
