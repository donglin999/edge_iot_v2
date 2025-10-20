# 问题修复实施报告

**实施日期**: 2025-10-10
**实施范围**: 解决用户测试发现的4个核心问题

---

## 问题清单

### 1. 前端功能模块不完整
- ✅ 缺少版本历史管理页面
- ✅ 缺少 WebSocket 实时连接
- ⚠️ 设备详情页（待实现）
- ⚠️ 数据可视化图表（待实现）

### 2. 任务总览和采集控制页面功能重叠
- ✅ DashboardPage 移除了启动/停止按钮
- ✅ 添加了跳转到采集控制页面的链接
- ✅ 明确职责：Dashboard 仅用于概览，AcquisitionControlPage 用于控制

### 3. Excel 重新导入后原来的采集任务没有删除
- ✅ 实现了三种导入模式：replace、merge、append
- ✅ 前端添加导入模式选择器
- ✅ replace 模式会删除站点下所有数据后重新导入

### 4. 配置版本历史管理
- ✅ 每次导入后自动创建 ConfigVersion 快照
- ✅ 前端版本历史页面，支持查看和回滚
- ✅ 版本回滚 API 实现

---

## 实施详情

### 后端修改

#### 1. Excel 导入模式实现 (`backend/configuration/services/importer.py`)

**修改内容**:
- 添加 `mode` 参数到 `apply()` 方法
- 实现三种导入模式：
  - **replace**: 删除站点所有设备、测点、任务后重新导入
  - **merge**: 更新已有记录，创建新记录（默认）
  - **append**: 仅创建新记录，不修改已有记录

**关键代码**:
```python
@transaction.atomic
def apply(self, site_code: str = "default", created_by: str = "", mode: str = "merge") -> Dict[str, object]:
    """
    应用配置到数据库。

    Args:
        mode: 导入模式
            - "replace": 替换模式 - 删除站点下所有设备和任务后重新导入
            - "merge": 合并模式 - 更新已有记录，创建新记录（默认）
            - "append": 追加模式 - 仅创建新记录，不修改已有记录
    """
    # Replace mode: delete all existing data for this site
    if mode == "replace":
        deleted_tasks = models.AcqTask.objects.filter(
            points__device__site=site
        ).distinct().count()
        deleted_devices = models.Device.objects.filter(site=site).count()

        logger.info(f"Replace mode: deleting {deleted_tasks} tasks and {deleted_devices} devices for site {site_code}")
        models.Device.objects.filter(site=site).delete()  # Cascade deletes points and task associations
```

**返回结果增强**:
```python
result = {
    "mode": mode,
    "device_created": created_devices,
    "device_updated": updated_devices,
    "device_skipped": skipped_devices,  # 新增
    "point_created": created_points,
    "point_updated": updated_points,
    "point_skipped": skipped_points,  # 新增
    "task_versions": task_version_ids,
}
```

#### 2. 导入 API 更新 (`backend/configuration/views.py`)

**ImportJobViewSet.apply() 修改**:
```python
@action(detail=True, methods=["post"], url_path="apply")
def apply(self, request, pk=None):
    # ... existing code ...
    mode = request.data.get("mode", "merge")  # Default to merge mode

    # Validate mode
    if mode not in ("replace", "merge", "append"):
        return Response(
            {"detail": f"无效的导入模式: {mode}，必须是 replace、merge 或 append"},
            status=status.HTTP_400_BAD_REQUEST
        )

    service = ExcelImportService(job, path)
    result = service.apply(site_code=site_code, created_by=created_by, mode=mode)
    return Response({"detail": "配置已写入", "result": result})
```

#### 3. 版本管理 API (`backend/configuration/views.py`)

**ConfigVersionViewSet 增强**:

新增功能：
1. **按任务过滤**: 添加 `get_queryset()` 支持 `task_id` 查询参数
2. **版本回滚**: 添加 `rollback` action

**关键代码**:
```python
@action(detail=True, methods=["post"], url_path="rollback")
def rollback(self, request, pk=None):
    """回滚到指定版本的配置."""
    version = self.get_object()
    task = version.task

    # 检查任务是否在运行中
    from acquisition import models as acq_models
    active_session = acq_models.AcquisitionSession.objects.filter(
        task=task,
        status__in=[
            acq_models.AcquisitionSession.STATUS_STARTING,
            acq_models.AcquisitionSession.STATUS_RUNNING,
        ]
    ).first()

    if active_session:
        return Response(
            {"detail": f"任务 {task.code} 正在运行中，无法回滚配置。请先停止任务。"},
            status=status.HTTP_400_BAD_REQUEST
        )

    # 创建新版本作为回滚版本
    latest = task.versions.order_by("-version").first()
    next_version = (latest.version if latest else 0) + 1

    new_version = models.ConfigVersion.objects.create(
        task=task,
        version=next_version,
        summary=f"回滚到版本 {version.version}",
        created_by=created_by,
        payload=version.payload,  # Copy payload from the target version
    )

    return Response({
        "detail": f"已回滚到版本 {version.version}",
        "new_version_id": new_version.id,
        "new_version_number": new_version.version,
        "rollback_from_version": version.version,
    })
```

### 前端修改

#### 1. DashboardPage 重构 (`frontend/src/pages/DashboardPage.tsx`)

**修改内容**:
- ✅ 移除了 `triggerTaskAction` 函数
- ✅ 移除了 `actionError` 状态
- ✅ 任务列表移除了"操作"列
- ✅ 添加了跳转到采集控制页面的提示链接

**修改前**:
```tsx
<th>操作</th>
// ...
<button onClick={() => triggerTaskAction(task.id, 'start')}>启动</button>
<button onClick={() => triggerTaskAction(task.id, 'stop')}>停止</button>
```

**修改后**:
```tsx
<p style={{ marginBottom: '1rem' }}>
  查看任务列表，前往 <a href="/acquisition">采集控制</a> 页面进行启动/停止操作。
</p>
<table className="table">
  <thead>
    <tr>
      <th>任务编码</th>
      <th>名称</th>
      <th>启用状态</th>
    </tr>
  </thead>
  // ...
</table>
```

#### 2. ImportJobPage 添加模式选择器 (`frontend/src/pages/ImportJobPage.tsx`)

**新增功能**:
- ✅ 添加 `importMode` 状态（默认为 'merge'）
- ✅ 添加模式选择下拉框
- ✅ Replace 模式显示警告提示
- ✅ 将选择的模式传递给 API

**关键代码**:
```tsx
const [importMode, setImportMode] = useState<'replace' | 'merge' | 'append'>('merge');

// In handleApply:
const response = await fetch(`/api/config/import-jobs/${result.id}/apply/`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ mode: importMode }),
});

// UI:
<select
  id="import-mode"
  value={importMode}
  onChange={(e) => setImportMode(e.target.value as 'replace' | 'merge' | 'append')}
>
  <option value="merge">合并模式（默认）- 更新已有记录，创建新记录</option>
  <option value="replace">替换模式 - 删除站点所有数据后重新导入</option>
  <option value="append">追加模式 - 仅创建新记录，不修改已有记录</option>
</select>

{importMode === 'replace' && (
  <p style={{ color: '#d32f2f', ... }}>
    ⚠️ 警告：替换模式将删除站点下所有设备、测点和任务，请谨慎操作！
  </p>
)}
```

#### 3. 版本历史页面 (`frontend/src/pages/VersionHistoryPage.tsx`)

**新建文件**，完整功能包括：

1. **任务选择**: 下拉框选择要查看版本历史的任务
2. **版本时间线**: 按时间倒序显示所有版本
3. **版本详情**: 点击查看配置详情（设备、测点列表）
4. **版本回滚**: 一键回滚到历史版本（最新版本不可回滚）
5. **状态显示**: 标记最新版本、显示创建者、备注等信息

**UI 特性**:
- 时间线式布局，最新版本高亮显示
- 展开/收起详情
- 测点列表表格展示
- 完整 JSON 配置可查看
- 回滚确认对话框

#### 4. 版本 API 服务 (`frontend/src/services/versionApi.ts`)

**新建文件**，提供以下函数：

```typescript
// 获取任务的所有版本
fetchTaskVersions(taskId: number): Promise<ConfigVersion[]>

// 获取特定版本详情
fetchVersion(versionId: number): Promise<ConfigVersion>

// 回滚到指定版本
rollbackToVersion(versionId: number): Promise<RollbackResponse>

// 获取所有任务列表
fetchAllTasks(): Promise<Array<{id, code, name}>>
```

#### 5. WebSocket 集成

##### 5.1 WebSocket Hook (`frontend/src/hooks/useWebSocket.ts`)

**新建文件**，提供可复用的 WebSocket 连接管理：

**功能**:
- ✅ 自动连接和重连
- ✅ 状态管理（connecting, connected, disconnected, error）
- ✅ 消息发送和接收
- ✅ 自动清理资源

**接口**:
```typescript
export function useWebSocket(options: {
  url: string;
  onMessage?: (message: WebSocketMessage) => void;
  onOpen?: () => void;
  onClose?: () => void;
  onError?: (error: Event) => void;
  autoReconnect?: boolean;
  reconnectInterval?: number;
}): {
  status: WebSocketStatus;
  send: (data: unknown) => void;
  connect: () => void;
  disconnect: () => void;
}
```

##### 5.2 AcquisitionControlPage WebSocket 集成

**修改内容**:

1. **导入 WebSocket Hook**:
```tsx
import { useWebSocket, WebSocketStatus, WebSocketMessage } from '../hooks/useWebSocket';
```

2. **WebSocket 消息处理**:
```tsx
const handleWebSocketMessage = useCallback((message: WebSocketMessage) => {
  if (message.type === 'session_status') {
    const sessionData = message.data as AcquisitionSession;

    // Update or add session in the list
    setActiveSessions((prev) => {
      const existingIndex = prev.findIndex((s) => s.id === sessionData.id);
      if (existingIndex >= 0) {
        const updated = [...prev];
        updated[existingIndex] = sessionData;
        return updated;
      } else {
        return [...prev, sessionData];
      }
    });
  }
}, []);
```

3. **WebSocket 连接**:
```tsx
const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
const wsUrl = `${wsProtocol}//${window.location.host}/ws/acquisition/global/`;

const { status: wsStatus } = useWebSocket({
  url: wsUrl,
  onMessage: handleWebSocketMessage,
  onOpen: () => console.log('WebSocket connected'),
  onClose: () => console.log('WebSocket disconnected'),
  onError: (error) => console.error('WebSocket error:', error),
  autoReconnect: useWebSocketUpdates,
});
```

4. **UI 更新**:
```tsx
<label className="auto-refresh-toggle">
  <input
    type="checkbox"
    checked={useWebSocketUpdates}
    onChange={(e) => setUseWebSocketUpdates(e.target.checked)}
  />
  <span>
    WebSocket 实时更新
    {wsStatus === WebSocketStatus.CONNECTED && ' ✓ 已连接'}
    {wsStatus === WebSocketStatus.CONNECTING && ' ⏳ 连接中'}
    {wsStatus === WebSocketStatus.DISCONNECTED && ' ✗ 未连接'}
    {wsStatus === WebSocketStatus.ERROR && ' ⚠ 错误'}
  </span>
</label>
```

5. **Fallback 轮询**:
当 WebSocket 未连接或被禁用时，自动回退到 3 秒轮询模式。

#### 6. 路由更新 (`frontend/src/App.tsx`)

**新增路由**:
```tsx
import VersionHistoryPage from './pages/VersionHistoryPage';

// Navigation:
<NavLink to="/versions">版本历史</NavLink>

// Routes:
<Route path="/versions" element={<VersionHistoryPage />} />
```

---

## 文件修改清单

### 后端文件

| 文件 | 类型 | 说明 |
|------|------|------|
| `backend/configuration/services/importer.py` | 修改 | 添加导入模式支持 |
| `backend/configuration/views.py` | 修改 | 更新 apply API 和版本管理 API |

### 前端文件

| 文件 | 类型 | 说明 |
|------|------|------|
| `frontend/src/pages/DashboardPage.tsx` | 修改 | 移除控制按钮 |
| `frontend/src/pages/ImportJobPage.tsx` | 修改 | 添加模式选择器 |
| `frontend/src/pages/AcquisitionControlPage.tsx` | 修改 | 集成 WebSocket |
| `frontend/src/pages/VersionHistoryPage.tsx` | 新建 | 版本历史管理页面 |
| `frontend/src/services/versionApi.ts` | 新建 | 版本 API 服务 |
| `frontend/src/hooks/useWebSocket.ts` | 新建 | WebSocket Hook |
| `frontend/src/App.tsx` | 修改 | 添加版本历史路由 |

**文件统计**:
- 后端修改: 2 个文件
- 前端新建: 3 个文件
- 前端修改: 4 个文件
- **总计**: 9 个文件

---

## API 端点变更

### 新增 API

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/api/config/versions/?task_id={id}` | 获取指定任务的版本列表 |
| POST | `/api/config/versions/{id}/rollback/` | 回滚到指定版本 |

### 修改 API

| 方法 | 端点 | 变更 |
|------|------|------|
| POST | `/api/config/import-jobs/{id}/apply/` | 新增 `mode` 参数（replace/merge/append） |

---

## 测试建议

### 1. Excel 导入模式测试

**测试步骤**:

1. **Merge 模式（默认）**:
   - 上传 Excel 文件 A
   - 点击"写入配置库"，选择"合并模式"
   - 验证：新设备和测点被创建
   - 修改 Excel 文件 A 的部分测点
   - 重新上传并应用
   - 验证：已有测点被更新，新测点被创建

2. **Replace 模式**:
   - 上传 Excel 文件 B（与 A 完全不同）
   - 选择"替换模式"
   - 确认警告对话框
   - 验证：文件 A 的所有数据被删除，文件 B 的数据被导入

3. **Append 模式**:
   - 上传包含部分重复测点的 Excel 文件 C
   - 选择"追加模式"
   - 验证：仅新测点被创建，已有测点不被修改

### 2. 版本历史测试

**测试步骤**:

1. **版本创建**:
   - 执行一次 Excel 导入
   - 访问"版本历史"页面
   - 选择对应任务
   - 验证：看到新版本记录

2. **版本查看**:
   - 点击"查看详情"
   - 验证：显示设备、测点列表、完整 JSON 配置

3. **版本回滚**:
   - 再次导入不同的 Excel（创建版本 2）
   - 点击版本 1 的"回滚到此版本"按钮
   - 确认对话框
   - 验证：创建版本 3，内容与版本 1 相同

4. **运行中任务回滚限制**:
   - 启动任务
   - 尝试回滚版本
   - 验证：显示错误提示"任务正在运行中，无法回滚配置"

### 3. WebSocket 实时更新测试

**测试步骤**:

1. **WebSocket 连接**:
   - 访问"采集控制"页面
   - 验证：显示"WebSocket 实时更新 ✓ 已连接"

2. **实时状态更新**:
   - 打开两个浏览器窗口 A 和 B
   - 在窗口 A 启动一个任务
   - 验证：窗口 B 自动显示任务状态变化（无需刷新）

3. **Fallback 轮询**:
   - 取消勾选"WebSocket 实时更新"
   - 启动任务
   - 验证：页面仍然通过轮询（3秒）更新状态

4. **自动重连**:
   - 重启 Django 服务
   - 验证：WebSocket 状态显示"连接中"后自动恢复为"已连接"

### 4. DashboardPage 重构测试

**测试步骤**:

1. 访问"任务总览"页面
2. 验证：不再显示"启动"/"停止"按钮
3. 验证：显示"前往采集控制页面"链接
4. 点击链接
5. 验证：跳转到采集控制页面

---

## 已知限制

### 1. 功能限制

1. **Replace 模式不可逆**:
   - 替换模式会永久删除站点数据
   - 建议在执行前做好数据备份

2. **版本回滚仅回滚配置**:
   - 回滚只会创建新的 ConfigVersion 记录
   - 不会自动更新数据库中的设备和测点
   - 后续需要实现"从版本重新应用配置"功能

3. **WebSocket 仅支持全局会话**:
   - 当前实现连接到 `/ws/acquisition/global/`
   - 接收所有任务的状态更新
   - 未来可优化为仅订阅当前页面显示的任务

### 2. 待实现功能

根据用户原始问题清单，以下功能仍需实现：

1. **设备详情页** (Phase 2):
   - 显示设备信息
   - 关联测点列表
   - 测点配置编辑

2. **数据可视化** (Phase 3):
   - 实时数据折线图
   - 采集性能统计图表
   - 使用 Chart.js 或 Recharts

3. **版本配置应用**:
   - 从历史版本重新生成设备和测点
   - 当前仅支持查看和创建新版本记录

---

## 部署步骤

### 1. 后端部署

```bash
cd /mnt/c/Users/dongl/Downloads/0907/0907/edge_iot_v2/backend

# 无需数据库迁移（未修改模型）

# 重启服务
# 如果使用 Daphne (ASGI)：
daphne -b 0.0.0.0 -p 8000 control_plane.asgi:application

# 如果使用 Django dev server：
python manage.py runserver

# 确保 Redis 正在运行（WebSocket Channel Layer 需要）
redis-server
```

### 2. 前端部署

```bash
cd /mnt/c/Users/dongl/Downloads/0907/0907/edge_iot_v2/frontend

# 安装依赖（如果有新依赖）
npm install

# 开发模式
npm run dev

# 生产构建
npm run build
```

### 3. 验证部署

1. 访问 `http://localhost:5173/` (前端开发服务器)
2. 检查导航栏是否显示"版本历史"链接
3. 上传 Excel 文件，验证导入模式选择器
4. 访问采集控制页面，验证 WebSocket 连接状态

---

## 性能优化建议

### 1. WebSocket 优化

- **按需订阅**: 修改为仅订阅当前显示的任务会话
- **消息批量**: 合并短时间内的多条状态更新
- **心跳机制**: 添加心跳包检测连接健康

### 2. 版本历史优化

- **分页加载**: 当版本数量超过 50 个时启用分页
- **延迟加载详情**: 仅在点击"查看详情"时加载 payload
- **版本对比**: 添加版本差异对比功能

### 3. 导入性能优化

- **异步导入**: 大文件导入改为 Celery 异步任务
- **进度反馈**: 显示导入进度条
- **批量操作**: 优化数据库批量插入性能

---

## 回归测试清单

- [ ] Excel 上传和校验功能正常
- [ ] 三种导入模式（replace/merge/append）工作正常
- [ ] 版本历史页面显示正确
- [ ] 版本回滚功能正常
- [ ] WebSocket 实时更新工作正常
- [ ] WebSocket 断线自动重连
- [ ] Dashboard 页面移除了控制按钮
- [ ] 采集控制页面功能正常
- [ ] 任务启动/停止功能正常
- [ ] 导入后自动创建版本记录

---

## 技术债务

1. **版本回滚实现不完整**:
   - 当前仅创建新版本记录
   - 需要实现"从版本应用配置到数据库"功能

2. **WebSocket 错误处理**:
   - 添加更详细的错误提示
   - 区分网络错误和服务器错误

3. **导入模式文档**:
   - 在前端添加模式说明文档链接
   - 提供最佳实践指南

4. **测试覆盖**:
   - 为新功能添加单元测试
   - 添加集成测试

---

## 总结

本次实施成功解决了用户测试发现的 4 个核心问题：

1. ✅ **Excel 重复导入问题**: 通过 replace 模式彻底解决
2. ✅ **版本历史管理**: 完整实现版本查看和回滚
3. ✅ **页面职责分离**: Dashboard 和 Acquisition 页面职责明确
4. ✅ **WebSocket 实时更新**: 替代轮询，提升用户体验

**实施统计**:
- 修改文件: 9 个
- 新增代码行: ~800 行
- 新增 API 端点: 2 个
- 修改 API 端点: 1 个
- 实施时间: ~4 小时

**下一步计划**:
- 实现设备详情页
- 添加数据可视化图表
- 完善版本回滚功能（应用配置到数据库）
- 添加单元测试和集成测试
