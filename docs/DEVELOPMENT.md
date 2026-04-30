# 开发指南

## 项目结构

```
edge_iot_v2/
├── backend/                # Django后端
│   ├── acquisition/       # 采集模块
│   ├── configuration/     # 配置模块
│   ├── storage/          # 存储模块
│   ├── protocols/        # 协议模块
│   ├── common/           # 公共模块
│   ├── monitoring/       # 监控模块
│   └── control_plane/    # Django配置
├── frontend/             # React前端
│   ├── src/
│   │   ├── components/  # 组件
│   │   ├── pages/       # 页面
│   │   ├── services/    # API服务
│   │   └── hooks/       # Hooks
├── mock/                # Mock设备
├── docs/                # 文档
└── docker-compose.yml   # Docker配置
```

## 后端开发

### 添加新协议

1. 在 `backend/acquisition/protocols/` 下创建新协议文件
2. 继承 `BaseProtocol` 接口
3. 实现必要方法
4. 在 `ProtocolRegistry` 中注册

**示例: 添加自定义协议**

```python
# backend/acquisition/protocols/custom.py
from .base import BaseProtocol, ProtocolRegistry

class CustomProtocol(BaseProtocol):
    """自定义协议实现"""

    def __init__(self, host: str, port: int, **kwargs):
        self.host = host
        self.port = port
        self.connection = None

    def connect(self) -> bool:
        """建立连接"""
        # 实现连接逻辑
        return True

    def disconnect(self) -> None:
        """断开连接"""
        if self.connection:
            self.connection.close()

    def read_points(self, points: List[Dict]) -> List[Dict]:
        """读取测点数据"""
        readings = []
        for point in points:
            # 读取逻辑
            readings.append({
                "point_code": point["code"],
                "value": self._read_value(point["address"]),
                "timestamp": datetime.now(),
                "quality": "good"
            })
        return readings

    def _read_value(self, address: str):
        """读取单个地址的值"""
        pass

# 注册协议
ProtocolRegistry.register("custom", CustomProtocol)
```

2. 在设备模型中设置协议类型

```python
# 使用时
protocol_type = "custom"
```

### 添加新存储

1. 在 `backend/storage/` 下创建新存储文件
2. 继承 `BaseStorage` 接口
3. 实现必要方法

```python
# backend/storage/timescale.py
from .base import BaseStorage, StorageRegistry

@StorageRegistry.register("timescale")
class TimescaleStorage(BaseStorage):
    """TimescaleDB存储实现"""

    def connect(self) -> bool:
        # 实现连接
        pass

    def write(self, data: List[Dict]) -> bool:
        # 实现写入
        pass

    def query(self, sql: str) -> List[Dict]:
        # 实现查询
        pass
```

### 运行测试

```bash
cd backend

# 运行所有测试
python3 -m pytest tests/ -v

# 运行特定测试
python3 -m pytest tests/test_protocols.py -v

# 运行带覆盖率的测试
python3 -m pytest tests/ --cov=. --cov-report=html
```

### Mock设备

启动Modbus TCP模拟设备:

```bash
# 启动Mock设备
python3 mock/modbus_mock_server.py --port 5020

# 或使用脚本
./mock/start_mock_modbus.sh
```

## 前端开发

### 项目结构

```
frontend/
├── src/
│   ├── components/        # 可复用组件
│   │   ├── RealtimeChart.tsx
│   │   ├── HistoricalTrendChart.tsx
│   │   └── acquisition/
│   │       └── TaskControlPanel.tsx
│   ├── pages/            # 页面组件
│   │   ├── DashboardPage.tsx
│   │   ├── DeviceListPage.tsx
│   │   └── ...
│   ├── services/         # API服务
│   │   ├── deviceApi.ts
│   │   ├── acquisitionApi.ts
│   │   └── dataApi.ts
│   └── hooks/            # 自定义Hooks
│       └── useWebSocket.ts
```

### 添加新页面

1. 创建页面组件:

```tsx
// src/pages/NewPage.tsx
import { useState, useEffect } from 'react';

export default function NewPage() {
  const [data, setData] = useState([]);

  useEffect(() => {
    // 加载数据
  }, []);

  return (
    <div>
      <h1>新页面</h1>
    </div>
  );
}
```

2. 在App.tsx中添加路由:

```tsx
import NewPage from './pages/NewPage';

function App() {
  return (
    <Routes>
      <Route path="/new-page" element={<NewPage />} />
    </Routes>
  );
}
```

### 添加API服务

```typescript
// src/services/newApi.ts
import axios from 'axios';

const api = axios.create({
  baseURL: '/api',
});

export const newApi = {
  getData: () => api.get('/endpoint/'),
  createData: (data: any) => api.post('/endpoint/', data),
};
```

### WebSocket使用

```typescript
import useWebSocket from './hooks/useWebSocket';

function MyComponent() {
  const { data, isConnected } = useWebSocket('/ws/acquisition/1/');

  if (!isConnected) {
    return <div>连接中...</div>;
  }

  return (
    <div>
      实时数据: {JSON.stringify(data)}
    </div>
  );
}
```

## 代码规范

### Python

- 遵循 PEP 8
- 使用类型注解
- 文档字符串使用 Google 风格

### TypeScript

- 遵循 ESLint 规则
- 优先使用函数组件
- 使用 Hooks 管理状态

## 调试技巧

### 后端调试

1. 使用Django调试模式:

```python
# settings.py
DEBUG = True
```

2. 查看详细日志:

```bash
python manage.py runserver --debug-sql
```

### 前端调试

1. 打开React DevTools
2. 使用console.log调试
3. 查看Network面板检查API请求

## 相关文档

- [系统架构](SYSTEM_ARCHITECTURE.md)
- [API参考](API_REFERENCE.md)
- [快速开始](QUICKSTART.md)
