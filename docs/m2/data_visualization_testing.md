# 数据可视化功能测试指南

## 功能概述

数据可视化功能已成功实现，包括：
1. **历史趋势图表** - 从 InfluxDB 查询并显示历史时序数据
2. **实时数据图表** - 通过 WebSocket 显示实时采集数据
3. **多种图表类型** - 折线图、面积图
4. **统计分析** - 自动计算最大值、最小值、平均值
5. **交互功能** - 时间范围选择、数据刷选（Brush）、CSV 导出

## 架构说明

### 数据流向
```
设备 → 采集服务 → InfluxDB → Django API → React 前端
                     ↓
                 WebSocket → React 前端（实时数据）
```

### 数据存储架构
- **PostgreSQL**: 配置数据（设备、测点、任务）
- **InfluxDB**: 实时时序数据（测点值、时间戳、质量标记）

## 已实现的组件

### 后端 API

#### 1. 历史数据查询接口
**端点**: `GET /api/acquisition/sessions/point-history/`

**参数**:
- `point_code`: 测点代码（必需）
- `start_time`: 开始时间（默认 `-1h`，支持相对时间如 `-2h`, `-24h`）
- `end_time`: 结束时间（默认 `now()`）
- `limit`: 返回数据点数量（默认 1000）

**响应示例**:
```json
{
    "point_code": "Device_Status",
    "start_time": "-2h",
    "end_time": "now()",
    "count": 37,
    "data": [
        {
            "timestamp": "2025-10-10T05:53:39Z",
            "value": 24.5829,
            "quality": "good"
        },
        ...
    ]
}
```

**实现位置**: `backend/acquisition/views.py:335-419`

**技术细节**:
- 使用 Docker CLI 调用 InfluxDB（解决外部认证问题的临时方案）
- 解析 InfluxDB CSV 格式输出
- 支持 Flux 查询语言

### 前端组件

#### 1. DataVisualizationPage
**位置**: `frontend/src/pages/DataVisualizationPage.tsx`

**功能**:
- 设备和测点选择
- 时间范围选择（1小时、4小时、24小时、7天、自定义）
- 图表类型切换（折线图、面积图）
- 统计信息显示
- CSV 数据导出

#### 2. HistoricalTrendChart
**位置**: `frontend/src/components/HistoricalTrendChart.tsx`

**功能**:
- 显示历史时序数据
- 支持数据缩放和刷选（Brush）
- 自动计算统计值
- 响应式布局

#### 3. RealtimeChart
**位置**: `frontend/src/components/RealtimeChart.tsx`

**功能**:
- WebSocket 实时数据流
- 滑动窗口显示（默认最近 50 个数据点）
- 连接状态指示
- 自动重连机制

#### 4. useWebSocket Hook
**位置**: `frontend/src/hooks/useWebSocket.ts`

**功能**:
- WebSocket 连接管理
- 自动重连（3秒间隔）
- 消息解析和分发
- 连接状态跟踪

#### 5. Data API Service
**位置**: `frontend/src/services/dataApi.ts`

**功能**:
- 类型安全的 API 调用
- 设备和测点查询
- 历史数据获取
- 统计数据获取

## 测试步骤

### 准备工作

确保所有服务运行正常：
```bash
# 检查服务状态
curl http://localhost:8000/api/config/devices/    # Django 后端
curl http://localhost:5173                        # 前端
docker ps | grep influxdb                         # InfluxDB
```

### 测试 1: API 端点测试

#### 1.1 查询最近 2 小时的数据
```bash
curl "http://localhost:8000/api/acquisition/sessions/point-history/?point_code=Device_Status&start_time=-2h&limit=5" | python3 -m json.tool
```

**期望结果**: 返回 5 个数据点，包含时间戳、值和质量

#### 1.2 查询最近 24 小时的所有数据
```bash
curl "http://localhost:8000/api/acquisition/sessions/point-history/?point_code=Device_Status&start_time=-24h&limit=100" | python3 -m json.tool
```

**期望结果**: 返回 37 个数据点（所有模拟数据）

#### 1.3 查询设备列表
```bash
curl "http://localhost:8000/api/config/devices/" | python3 -m json.tool
```

**期望结果**: 返回设备列表，包含设备 ID=2 的"海天注塑机"

#### 1.4 查询测点列表
```bash
curl "http://localhost:8000/api/config/devices/2/points/" | python3 -m json.tool
```

**期望结果**: 返回测点列表，包含 "Device_Status" 测点

### 测试 2: 前端界面测试

#### 2.1 访问数据可视化页面
1. 打开浏览器访问 http://localhost:5173
2. 点击顶部导航栏的"数据可视化"链接
3. 验证页面加载成功

**期望结果**:
- 页面显示设备选择下拉框
- 显示时间范围选择按钮
- 显示图表类型选择

#### 2.2 选择设备和测点
1. 在"选择设备"下拉框中选择"海天注塑机 (modbustcp-10.41.68.49-4196)"
2. 在"选择测点"下拉框中选择"Device_Status - 设备状态"
3. 选择时间范围"最近 24 小时"
4. 点击"查询"按钮

**期望结果**:
- 图表显示 37 个数据点
- 时间轴显示从昨天到今天的时间
- 数值轴显示温度范围（约 20-30）
- 统计信息显示：
  - 数据点数：37
  - 最大值：约 30
  - 最小值：约 20
  - 平均值：约 25

#### 2.3 测试图表交互
1. **缩放功能**: 使用图表底部的 Brush 控件拖动选择时间段
2. **图表类型切换**: 切换到"面积图"查看效果
3. **时间范围切换**: 切换到"最近 1 小时"、"最近 4 小时"等不同范围

**期望结果**:
- Brush 缩放功能正常工作
- 图表类型切换平滑
- 不同时间范围查询返回相应数据

#### 2.4 测试 CSV 导出
1. 点击"导出 CSV"按钮
2. 检查下载的文件

**期望结果**:
- 文件名格式: `Device_Status_YYYY-MM-DD_HH-MM-SS.csv`
- CSV 包含三列: 时间戳, 数值, 质量
- 数据完整无误

### 测试 3: 实时数据测试

#### 3.1 启动采集任务
```bash
# 查看可用任务
curl "http://localhost:8000/api/config/tasks/" | python3 -m json.tool

# 启动任务（假设任务 ID 为 1）
curl -X POST "http://localhost:8000/api/acquisition/tasks/1/start/" | python3 -m json.tool
```

#### 3.2 查看活动会话
```bash
curl "http://localhost:8000/api/acquisition/sessions/active/" | python3 -m json.tool
```

**期望结果**: 返回活动会话列表，包含会话 ID

#### 3.3 测试实时图表
1. 在数据可视化页面切换到"实时监控"标签
2. 输入活动会话 ID
3. 点击"开始监控"

**期望结果**:
- WebSocket 连接状态显示"已连接"
- 图表实时更新显示新数据点
- 数据点以滑动窗口方式显示（最近 50 个点）

### 测试 4: InfluxDB 数据验证

#### 4.1 直接查询 InfluxDB
```bash
# 统计数据点数量
docker exec influxdb influx query 'from(bucket:"iot-data") |> range(start: -24h) |> filter(fn: (r) => r["point"] == "Device_Status") |> count()' --raw

# 查看前 5 个数据点
docker exec influxdb influx query 'from(bucket:"iot-data") |> range(start: -24h) |> filter(fn: (r) => r["point"] == "Device_Status") |> limit(n: 5)' --raw
```

**期望结果**:
- 计数显示 37 个数据点
- 数据点包含时间戳、值、设备信息等字段

## 当前数据状态

### 模拟数据
- **测点**: Device_Status
- **设备**: mock_device_001
- **数据量**: 37 个数据点
- **时间范围**: 2025-10-10 05:53:39 - 06:49:20 (约 1 小时)
- **数值范围**: 20.0 - 30.0（模拟温度）
- **采样间隔**: 约 60 秒

### 数据点示例
```json
{
    "timestamp": "2025-10-10T05:53:39Z",
    "value": 24.5829,
    "quality": "good"
}
```

## 已知问题和解决方案

### 问题 1: InfluxDB 外部认证失败
**症状**: Python influxdb-client 库和 curl 从容器外部连接时返回 401 Unauthorized

**原因**: Docker 网络或 InfluxDB 配置问题（具体原因待查）

**解决方案**: 使用 `docker exec influxdb influx query` 命令从 Django 调用（临时方案）

**代码位置**: `backend/acquisition/views.py:353-369`

### 问题 2: CSV 格式解析
**症状**: 初始实现无法正确解析 InfluxDB 的 CSV 输出

**原因**: InfluxDB `--raw` 输出格式特殊：
- 元数据行以 `#` 开头
- 列标题行以 `,result,table,` 开头
- 数据行以 `,,` 开头

**解决方案**: 更新 CSV 解析逻辑，正确识别标题和数据行

**代码位置**: `backend/acquisition/views.py:371-406`

## 后续改进计划

### 短期
1. 添加更多图表类型（散点图、柱状图、饼图）
2. 实现测点对比功能（多条曲线）
3. 添加数据聚合选项（平均值、最大值、最小值）
4. 改进错误提示和加载状态

### 中期
1. 解决 InfluxDB 外部认证问题，改用 Python 客户端
2. 添加数据告警和阈值设置
3. 实现数据下钻和详细分析
4. 添加数据质量监控面板

### 长期
1. 添加自定义仪表盘功能
2. 实现数据报表生成
3. 集成机器学习预测模型
4. 支持数据导出到多种格式（PDF、Excel）

## 技术栈

- **后端**: Django REST Framework 4.2.16, InfluxDB Python Client 1.42.0
- **前端**: React 18, TypeScript 5.2, Recharts 2.10, React Router 6
- **数据库**: PostgreSQL 13, InfluxDB 2.x
- **消息队列**: Redis, Celery
- **容器化**: Docker

## 文件清单

### 后端文件
- `backend/acquisition/views.py` - API 视图（包含 point_history 端点）
- `backend/storage/influxdb.py` - InfluxDB 存储后端
- `backend/.env` - InfluxDB 配置

### 前端文件
- `frontend/src/pages/DataVisualizationPage.tsx` - 主页面组件
- `frontend/src/components/HistoricalTrendChart.tsx` - 历史趋势图表组件
- `frontend/src/components/RealtimeChart.tsx` - 实时图表组件
- `frontend/src/hooks/useWebSocket.ts` - WebSocket 管理 Hook
- `frontend/src/services/dataApi.ts` - API 服务层
- `frontend/src/App.tsx` - 路由配置

### 文档
- `docs/m2/data_visualization_testing.md` - 本文档
- `docs/m2/changelog.md` - 功能变更日志

## 测试报告模板

```markdown
## 数据可视化功能测试报告

### 测试环境
- 日期: YYYY-MM-DD
- 测试人: XXX
- 浏览器: Chrome/Firefox/Safari 版本
- 后端版本: Git Commit Hash

### 测试结果

#### 1. API 端点测试
- [ ] 历史数据查询 - PASS/FAIL
- [ ] 设备列表查询 - PASS/FAIL
- [ ] 测点列表查询 - PASS/FAIL

#### 2. 前端界面测试
- [ ] 页面加载 - PASS/FAIL
- [ ] 设备测点选择 - PASS/FAIL
- [ ] 图表显示 - PASS/FAIL
- [ ] 统计计算 - PASS/FAIL
- [ ] CSV 导出 - PASS/FAIL

#### 3. 实时数据测试
- [ ] WebSocket 连接 - PASS/FAIL
- [ ] 实时数据更新 - PASS/FAIL

#### 4. InfluxDB 数据验证
- [ ] 数据点计数 - PASS/FAIL
- [ ] 数据完整性 - PASS/FAIL

### 发现的问题
1. 问题描述
   - 重现步骤
   - 期望结果
   - 实际结果
   - 错误日志

### 建议和改进
- 建议 1
- 建议 2
```

## 联系信息

如有问题或建议，请联系开发团队。
