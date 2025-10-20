# 数据可视化功能实现报告

## 概述

本文档记录了数据可视化功能的完整实现过程，包括实时数据图表和历史趋势分析。

**实施时间**: 2025-10-10
**功能状态**: ✅ 已完成
**估计工时**: 12小时
**实际工时**: ~3小时

---

## 功能特性

### 1. 实时数据可视化
- **WebSocket实时推送**: 通过Django Channels实现数据实时更新
- **动态折线图**: 使用Recharts展示最近50个数据点
- **连接状态指示**: 显示WebSocket连接状态（连接中/已连接/断开/错误）
- **自动滚动**: 保持最新数据可见

### 2. 历史数据趋势分析
- **多种图表类型**: 支持折线图和面积图
- **时间范围选择**: 1小时、6小时、24小时或自定义范围
- **数据统计**: 自动计算最大值、最小值、平均值
- **交互式缩放**: Brush组件支持局部放大查看
- **数据导出**: 支持导出为CSV格式

### 3. 数据筛选与管理
- **会话选择**: 选择活跃的采集会话进行实时监控
- **测点选择**: 从所有可用测点中选择要查看的数据
- **时间范围**: 灵活的时间范围选择
- **图表类型**: 切换折线图或面积图

---

## 技术架构

### Backend API增强

#### 新增API端点

**GET `/api/acquisition/sessions/point-history/`**
- 功能: 查询测点历史数据
- 参数:
  - `point_code` (required): 测点编码
  - `start_time` (optional): 开始时间 (ISO 8601)
  - `end_time` (optional): 结束时间 (ISO 8601)
  - `limit` (optional): 最大数据点数 (默认1000)
- 返回:
  ```json
  {
    "point_code": "Temperature_01",
    "start_time": "2025-10-10T00:00:00Z",
    "end_time": "2025-10-10T12:00:00Z",
    "count": 100,
    "data": [
      {"timestamp": "2025-10-10T00:00:00Z", "value": 25.5, "quality": "good"},
      ...
    ]
  }
  ```

**GET `/api/acquisition/sessions/active/`**
- 功能: 获取所有活跃的采集会话
- 返回: 运行中和启动中的会话列表

### Frontend架构

#### 1. API服务层 (`frontend/src/services/dataApi.ts`)

**核心函数**:
```typescript
// 历史数据查询
fetchPointHistory(pointCode, startTime?, endTime?, limit?)

// 会话管理
fetchActiveSessions()
fetchSessions(limit)
fetchSession(sessionId)
fetchSessionDataPoints(sessionId, limit, offset)

// 任务控制
startTask(taskId)
stopSession(sessionId, reason?)
```

**类型定义**:
- `DataPoint`: 数据点接口
- `PointHistoryResponse`: 历史数据响应
- `AcquisitionSession`: 采集会话接口
- `SessionDataPoint`: 会话数据点接口

#### 2. 可视化组件

##### RealtimeChart 组件
**路径**: `frontend/src/components/RealtimeChart.tsx`

**Props**:
- `sessionId`: 会话ID
- `title`: 图表标题
- `maxDataPoints`: 最大显示点数 (默认50)
- `height`: 图表高度 (默认300px)

**特性**:
- 使用 `useWebSocket` Hook连接实时数据
- 自动限制数据点数量，保持性能
- 显示连接状态指示器
- 自定义Tooltip显示详细信息

**WebSocket消息处理**:
```typescript
{
  type: 'data_point',
  data: {
    session_id: number,
    point_code: string,
    timestamp: string,
    value: number,
    quality: string
  }
}
```

##### HistoricalTrendChart 组件
**路径**: `frontend/src/components/HistoricalTrendChart.tsx`

**Props**:
- `pointCode`: 测点编码
- `startTime`: 开始时间
- `endTime`: 结束时间
- `title`: 图表标题
- `height`: 图表高度 (默认400px)
- `chartType`: 'line' | 'area'
- `showBrush`: 是否显示缩放条

**特性**:
- 自动计算统计信息（最大/最小/平均值）
- 支持Brush交互式缩放
- 响应式设计
- 加载和错误状态处理

#### 3. 数据可视化页面
**路径**: `frontend/src/pages/DataVisualizationPage.tsx`

**主要区块**:
1. **控制面板**
   - 会话选择下拉框
   - 测点选择下拉框
   - 时间范围选择
   - 图表类型选择
   - 自定义时间范围输入
   - 导出CSV按钮

2. **实时监控图表**
   - 显示选中会话的实时数据
   - WebSocket自动更新

3. **历史趋势图表**
   - 显示选中测点的历史数据
   - 支持时间范围筛选

4. **会话列表表格**
   - 显示最近10个会话
   - 状态标签（运行中/已停止/错误）

---

## 文件清单

### Backend文件
| 文件路径 | 修改类型 | 说明 |
|---------|---------|------|
| `backend/acquisition/views.py` | 修改 | 新增 `point_history` 端点 |

### Frontend文件
| 文件路径 | 修改类型 | 行数 | 说明 |
|---------|---------|------|------|
| `frontend/src/services/dataApi.ts` | 新建 | 172 | 数据可视化API服务 |
| `frontend/src/services/deviceApi.ts` | 修改 | +9 | 新增 `fetchDevices` 函数 |
| `frontend/src/components/RealtimeChart.tsx` | 新建 | 175 | 实时数据图表组件 |
| `frontend/src/components/HistoricalTrendChart.tsx` | 新建 | 264 | 历史趋势图表组件 |
| `frontend/src/pages/DataVisualizationPage.tsx` | 新建 | 343 | 数据可视化主页面 |
| `frontend/src/App.tsx` | 修改 | +2 | 新增路由和导航 |

**新增代码总计**: ~960行

---

## 依赖项

### 新增NPM包
```json
{
  "recharts": "^2.x.x"  // 已安装
}
```

### 现有依赖
- React Router (路由)
- TypeScript (类型安全)
- Vite (构建工具)

---

## API调用流程

### 实时数据流

```
用户选择会话
    ↓
建立WebSocket连接
ws://localhost:8000/ws/acquisition/sessions/{session_id}/
    ↓
接收实时数据推送
    ↓
更新图表显示
```

### 历史数据流

```
用户选择测点 + 时间范围
    ↓
GET /api/acquisition/sessions/point-history/
    ↓
渲染历史趋势图
    ↓
(可选) 导出CSV
```

---

## 性能优化

### 1. 数据点限制
- 实时图表: 最多50个数据点（滚动窗口）
- 历史图表: 最多1000个数据点

### 2. 查询优化
- 使用索引字段查询 (point_code, timestamp)
- 并行加载多个API请求 (Promise.all)

### 3. 渲染优化
- Recharts默认优化渲染性能
- 禁用实时图表动画 (`isAnimationActive={false}`)
- 响应式容器 (`ResponsiveContainer`)

---

## 使用指南

### 1. 查看实时数据

1. 访问 http://localhost:5173/data
2. 从"实时会话"下拉框选择活跃的会话
3. 实时图表将自动显示数据流

### 2. 查看历史趋势

1. 从"测点选择"下拉框选择要查看的测点
2. 选择时间范围（1h/6h/24h/自定义）
3. 选择图表类型（折线图/面积图）
4. 历史趋势图将自动加载

### 3. 导出数据

1. 选择测点和时间范围
2. 点击"导出数据 CSV"按钮
3. CSV文件将自动下载

---

## 已知限制

### 1. WebSocket连接限制
- 需要Redis支持（Channel Layer后端）
- 同时连接数受服务器限制

### 2. 数据量限制
- 单次查询最多1000个数据点
- 超大数据集需要分批查询

### 3. 浏览器兼容性
- 需要支持WebSocket的现代浏览器
- 建议使用Chrome/Firefox/Edge最新版本

---

## 测试建议

### 功能测试

1. **实时数据测试**
   - [ ] 启动采集会话
   - [ ] 选择会话后图表显示实时数据
   - [ ] WebSocket连接状态正确显示
   - [ ] 数据点超过50个后自动滚动

2. **历史数据测试**
   - [ ] 选择测点后显示历史数据
   - [ ] 时间范围筛选正确
   - [ ] 统计信息计算准确
   - [ ] Brush缩放功能正常

3. **数据导出测试**
   - [ ] CSV导出包含正确数据
   - [ ] 文件名格式正确
   - [ ] 时间格式为本地化显示

### 性能测试

1. **大数据量测试**
   - 查询1000个数据点的响应时间
   - 实时推送频率测试

2. **并发测试**
   - 多个用户同时查看不同会话
   - WebSocket连接稳定性

---

## 后续优化建议

### 短期优化 (1-2天)

1. **数据采样**: 对于超过1000个点的数据集，实现智能采样
2. **缓存优化**: 添加前端缓存避免重复请求
3. **错误重试**: 网络错误自动重试机制

### 中期优化 (1周)

1. **多测点对比**: 在同一图表中显示多个测点
2. **数据聚合**: 支持按分钟/小时聚合数据
3. **告警阈值线**: 在图表上显示告警阈值

### 长期优化 (2-4周)

1. **高级分析**: 数据趋势预测、异常检测
2. **仪表盘定制**: 用户自定义图表布局
3. **移动端优化**: 响应式设计优化

---

## 故障排查

### 问题: WebSocket连接失败

**可能原因**:
- Redis未运行
- Channel Layer配置错误
- 防火墙阻止WebSocket

**解决方案**:
```bash
# 检查Redis
redis-cli ping

# 检查Django Channels配置
python manage.py shell
>>> from channels.layers import get_channel_layer
>>> channel_layer = get_channel_layer()
>>> channel_layer is not None  # 应返回True
```

### 问题: 历史数据为空

**可能原因**:
- 数据库中没有数据点记录
- 时间范围选择错误
- 测点编码不正确

**解决方案**:
```bash
# 检查数据点数量
curl "http://localhost:8000/api/acquisition/sessions/point-history/?point_code=Device_Status"
```

### 问题: 图表不显示

**可能原因**:
- Recharts未正确安装
- 浏览器控制台有JavaScript错误

**解决方案**:
```bash
# 重新安装依赖
cd frontend
npm install recharts
npm run dev
```

---

## 总结

数据可视化功能已成功实现，提供了以下核心能力：

✅ 实时数据监控（WebSocket推送）
✅ 历史趋势分析（多种时间范围）
✅ 交互式图表（缩放、统计）
✅ 数据导出（CSV格式）
✅ 响应式设计（自适应布局）

该功能大幅提升了用户体验，使数据采集结果更加直观和易于分析。
