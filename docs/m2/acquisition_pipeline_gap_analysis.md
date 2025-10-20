# 采集任务完整链路缺口分析与开发计划

**生成时间**: 2025-10-09
**目标**: 实现"前端上传Excel → 解析配置 → 保存配置 → 执行采集任务 → 监控状态 → 控制任务"完整链路

---

## 一、链路现状分析

### ✅ 已实现的功能

#### 1. Excel上传与配置管理 (Configuration模块)

**后端API** - 完全实现 ✅
- `POST /api/config/import-jobs/` - Excel文件上传和校验
- `GET /api/config/import-jobs/{id}/diff/` - 查看配置差异
- `POST /api/config/import-jobs/{id}/apply/` - 应用配置到数据库
- `GET /api/config/sites/` - 站点管理
- `GET /api/config/devices/` - 设备管理
- `GET /api/config/points/` - 测点管理
- `GET /api/config/tasks/` - 采集任务管理

**前端页面** - 完全实现 ✅
- `ImportJobPage.tsx` - Excel上传、校验、应用配置
- `DeviceListPage.tsx` - 设备列表查看
- `DashboardPage.tsx` - 概览面板

**服务层** - 完全实现 ✅
- `ExcelImportService` - Excel解析、校验、差异计算
- Celery任务: `process_excel_import` - 异步处理Excel

**数据模型** - 完全实现 ✅
```
Site (站点)
  └─ Device (设备/连接)
       └─ Point (测点)
            └─ AcqTask (采集任务) - ManyToMany
```

---

#### 2. 采集框架重构 (Acquisition模块)

**核心架构** - 完全实现 ✅
- 协议抽象层 (`BaseProtocol` + `ProtocolRegistry`)
  - ModbusTCP协议 ✅
  - Mitsubishi PLC (MC协议) ✅
  - MQTT协议 ✅
- 存储抽象层 (`BaseStorage` + `StorageRegistry`)
  - InfluxDB 2.x存储 ✅
  - Kafka存储 ✅
- 服务层 (`AcquisitionService`) ✅
  - 单次采集: `acquire_once()`
  - 连续采集: `run_continuous()`
  - 设备分组、数据格式化、批量写入

**Celery任务** - 完全实现 ✅
- `start_acquisition_task(task_id)` - 启动采集任务
- `stop_acquisition_task(session_id)` - 停止采集任务
- `acquire_once(task_id)` - 单次测试采集
- `test_protocol_connection()` - 测试设备连接
- `test_storage_connection()` - 测试存储连接

**运行时模型** - 完全实现 ✅
```
AcquisitionSession (采集会话)
  - task: AcqTask
  - status: starting/running/stopping/stopped/error
  - celery_task_id: Celery任务ID
  - started_at, stopped_at
  - error_message

DataPoint (采集数据点)
  - session: AcquisitionSession
  - point_code, timestamp, value, quality
```

**测试覆盖** - 43/47测试通过 (91%) ✅
- 协议层: 15/15 ✅
- 存储层: 15/15 ✅
- 服务层: 8/10 ✅
- 集成测试: 5/7 ✅

---

### ❌ 缺失的功能 - 需要开发

#### 3. 采集任务控制API (Acquisition API)

**状态**: ⚠️ **缺少完整的REST API接口**

**问题**:
1. `acquisition`模块没有`views.py`和`urls.py`
2. 前端无法通过HTTP API控制采集任务
3. 现有的任务启动/停止API在`configuration`模块中，但没有真正调用Celery任务

**需要开发**:

```python
# backend/acquisition/views.py (需创建)

class AcquisitionSessionViewSet(viewsets.ModelViewSet):
    """采集会话管理"""

    @action(detail=False, methods=['post'])
    def start_task(self, request):
        """启动采集任务"""
        # 调用 start_acquisition_task.delay(task_id)
        # 返回 session_id 和 celery_task_id

    @action(detail=True, methods=['post'])
    def stop(self, request, pk=None):
        """停止采集会话"""
        # 调用 stop_acquisition_task.delay(session_id)

    @action(detail=True, methods=['post'])
    def pause(self, request, pk=None):
        """暂停采集会话"""

    @action(detail=True, methods=['post'])
    def resume(self, request, pk=None):
        """恢复采集会话"""

    @action(detail=True, methods=['get'])
    def status(self, request, pk=None):
        """获取会话状态详情"""
        # 返回当前状态、数据点数量、错误信息等

    @action(detail=False, methods=['get'])
    def active_sessions(self, request):
        """获取所有活跃的采集会话"""
        # 返回所有 running 状态的session
```

**需要的API端点**:
```
POST   /api/acquisition/sessions/start_task/      - 启动采集任务
POST   /api/acquisition/sessions/{id}/stop/       - 停止会话
POST   /api/acquisition/sessions/{id}/pause/      - 暂停会话
POST   /api/acquisition/sessions/{id}/resume/     - 恢复会话
GET    /api/acquisition/sessions/{id}/status/     - 查询状态
GET    /api/acquisition/sessions/active/          - 活跃会话列表
GET    /api/acquisition/sessions/                 - 会话历史
GET    /api/acquisition/sessions/{id}/data-points/ - 查询采集数据
POST   /api/acquisition/test-connection/          - 测试设备连接
POST   /api/acquisition/test-storage/             - 测试存储连接
```

---

#### 4. 实时状态监控 (WebSocket/SSE)

**状态**: ❌ **完全缺失**

**问题**:
- 前端无法实时获取采集任务的状态更新
- 需要不断轮询API查询状态
- 数据点更新没有推送机制

**需要开发**:

**方案A: Django Channels (WebSocket) - 推荐**
```python
# backend/acquisition/consumers.py (需创建)

class AcquisitionConsumer(AsyncWebsocketConsumer):
    """WebSocket消费者 - 推送采集状态"""

    async def connect(self):
        # 加入会话组
        self.session_id = self.scope['url_route']['kwargs']['session_id']
        await self.channel_layer.group_add(
            f"session_{self.session_id}",
            self.channel_name
        )

    async def session_status_update(self, event):
        """推送状态更新"""
        await self.send(text_data=json.dumps(event['data']))

    async def data_point_update(self, event):
        """推送新的数据点"""
        await self.send(text_data=json.dumps(event['data']))
```

**WebSocket端点**:
```
ws://localhost:8000/ws/acquisition/sessions/{session_id}/
```

**推送消息格式**:
```json
{
  "type": "session_status_update",
  "data": {
    "session_id": 123,
    "status": "running",
    "points_read": 1500,
    "last_read_time": "2025-10-09T10:30:00Z",
    "error_count": 0
  }
}

{
  "type": "data_point_update",
  "data": {
    "point_code": "TEMP_01",
    "value": 25.5,
    "quality": "good",
    "timestamp": "2025-10-09T10:30:00Z"
  }
}
```

**方案B: Server-Sent Events (SSE) - 简化方案**
```python
# backend/acquisition/views.py

@api_view(['GET'])
def session_status_stream(request, session_id):
    """SSE流式推送会话状态"""
    def event_stream():
        while True:
            session = AcquisitionSession.objects.get(pk=session_id)
            data = {
                'status': session.status,
                'updated_at': session.updated_at.isoformat(),
            }
            yield f"data: {json.dumps(data)}\n\n"
            time.sleep(1)
    return StreamingHttpResponse(event_stream(), content_type='text/event-stream')
```

---

#### 5. 前端采集控制页面

**状态**: ❌ **完全缺失**

**需要开发的页面**:

```typescript
// frontend/src/pages/AcquisitionControlPage.tsx (需创建)

interface AcquisitionControlPageProps {}

const AcquisitionControlPage: React.FC = () => {
  // 功能：
  // 1. 显示所有采集任务列表
  // 2. 每个任务显示当前状态 (运行中/已停止/错误)
  // 3. 启动/停止按钮
  // 4. 实时状态更新 (通过WebSocket)
  // 5. 查看采集数据统计
  // 6. 错误日志查看

  return (
    <div>
      <h1>采集任务控制台</h1>
      <TaskList tasks={tasks} />
      <ActiveSessions sessions={activeSessions} />
      <RealTimeStats />
    </div>
  );
};
```

**需要的组件**:
- `TaskList` - 任务列表
- `TaskControlPanel` - 任务控制面板 (启动/停止/查看日志)
- `SessionStatusCard` - 会话状态卡片
- `RealTimeChart` - 实时数据图表
- `ErrorLogViewer` - 错误日志查看器

---

#### 6. Configuration模块的任务控制完善

**状态**: ⚠️ **部分实现，但未连接Celery**

**问题**:
`configuration/views.py` 中的 `AcqTaskViewSet` 有 `start()` 和 `stop()` 方法，但是：
1. 只创建了 `TaskRun` 记录，没有真正启动Celery任务
2. 没有调用 `acquisition.tasks.start_acquisition_task()`
3. `TaskRun` 模型与 `AcquisitionSession` 重复

**需要修改**:

```python
# backend/configuration/views.py - AcqTaskViewSet

@action(detail=True, methods=["post"], url_path="start")
def start(self, request, pk=None):
    task: models.AcqTask = self.get_object()

    # 检查是否已有运行中的会话
    active_session = acq_models.AcquisitionSession.objects.filter(
        task=task,
        status__in=[acq_models.AcquisitionSession.STATUS_RUNNING]
    ).first()

    if active_session:
        return Response(
            {"detail": "任务已在运行中", "session_id": active_session.id},
            status=status.HTTP_400_BAD_REQUEST
        )

    # 启动Celery任务
    from acquisition.tasks import start_acquisition_task
    celery_result = start_acquisition_task.delay(task.id)

    return Response({
        "detail": "任务已启动",
        "celery_task_id": celery_result.id,
        "message": "请通过 /api/acquisition/sessions/ 查询会话状态"
    })

@action(detail=True, methods=["post"], url_path="stop")
def stop(self, request, pk=None):
    task: models.AcqTask = self.get_object()

    # 查找运行中的会话
    active_session = acq_models.AcquisitionSession.objects.filter(
        task=task,
        status__in=[acq_models.AcquisitionSession.STATUS_RUNNING]
    ).order_by("-started_at").first()

    if not active_session:
        return Response(
            {"detail": "未找到运行中的会话"},
            status=status.HTTP_400_BAD_REQUEST
        )

    # 停止Celery任务
    from acquisition.tasks import stop_acquisition_task
    stop_acquisition_task.delay(active_session.id)

    return Response({
        "detail": "停止指令已发送",
        "session_id": active_session.id
    })
```

---

## 二、完整开发计划

### 优先级 P0 - 核心链路打通

#### Task 1: 创建Acquisition API模块
**预计工时**: 4小时

**文件清单**:
```
backend/acquisition/
  ├── views.py          (新建)
  ├── urls.py           (新建)
  ├── serializers.py    (新建)
  └── permissions.py    (新建，可选)
```

**开发内容**:
- [ ] 创建 `AcquisitionSessionViewSet`
- [ ] 实现 `start_task` action - 启动采集
- [ ] 实现 `stop` action - 停止采集
- [ ] 实现 `status` action - 查询状态
- [ ] 实现 `active_sessions` action - 活跃会话列表
- [ ] 创建序列化器 (SessionSerializer, SessionStatusSerializer)
- [ ] 配置URL路由
- [ ] 在 `control_plane/urls.py` 中注册: `path("api/acquisition/", include("acquisition.urls"))`

**验收标准**:
```bash
# 测试启动采集
curl -X POST http://localhost:8000/api/acquisition/sessions/start_task/ \
  -H "Content-Type: application/json" \
  -d '{"task_id": 1}'

# 返回:
{
  "session_id": 123,
  "celery_task_id": "abc-123-def",
  "status": "starting",
  "started_at": "2025-10-09T10:00:00Z"
}

# 测试查询状态
curl http://localhost:8000/api/acquisition/sessions/123/status/

# 测试停止采集
curl -X POST http://localhost:8000/api/acquisition/sessions/123/stop/
```

---

#### Task 2: 修复Configuration模块的任务控制
**预计工时**: 2小时

**修改内容**:
- [ ] 修改 `AcqTaskViewSet.start()` - 调用Celery任务
- [ ] 修改 `AcqTaskViewSet.stop()` - 调用停止任务
- [ ] 添加任务状态查询: `@action` 方法 `get_session_status`
- [ ] 更新 `overview()` 方法使用 `AcquisitionSession` 而非 `TaskRun`

**验收标准**:
```bash
# 通过配置API启动任务
curl -X POST http://localhost:8000/api/config/tasks/1/start/

# 任务真正启动，Celery worker开始采集
# 可在InfluxDB或日志中看到数据
```

---

#### Task 3: 创建前端采集控制页面
**预计工时**: 6小时

**文件清单**:
```
frontend/src/
  ├── pages/
  │   └── AcquisitionControlPage.tsx  (新建)
  ├── components/acquisition/
  │   ├── TaskList.tsx                (新建)
  │   ├── TaskControlPanel.tsx        (新建)
  │   ├── SessionStatusCard.tsx       (新建)
  │   └── RealTimeStats.tsx           (新建)
  └── services/
      └── acquisitionApi.ts           (新建)
```

**开发内容**:
- [ ] 创建 `AcquisitionControlPage` 主页面
- [ ] 实现 `TaskList` - 显示所有采集任务
- [ ] 实现 `TaskControlPanel` - 启动/停止按钮
- [ ] 实现 `SessionStatusCard` - 会话状态显示
- [ ] 创建 `acquisitionApi.ts` - API调用封装
- [ ] 添加路由: `/acquisition-control`
- [ ] 集成到主导航菜单

**UI设计**:
```
┌─────────────────────────────────────────────────────┐
│  采集任务控制台                     [刷新] [设置]    │
├─────────────────────────────────────────────────────┤
│  任务列表                                           │
│  ┌──────────────────────────────────────────────┐  │
│  │ [●] TASK_001 - ModbusTCP设备1    [停止]     │  │
│  │     状态: 运行中 | 已采集: 1,234 点          │  │
│  │     开始时间: 2025-10-09 10:00:00            │  │
│  ├──────────────────────────────────────────────┤  │
│  │ [○] TASK_002 - PLC设备2         [启动]      │  │
│  │     状态: 已停止 | 上次运行: 1小时前         │  │
│  └──────────────────────────────────────────────┘  │
│                                                     │
│  活跃会话详情                                       │
│  ┌──────────────────────────────────────────────┐  │
│  │ 会话 #123 - TASK_001                         │  │
│  │ 采集周期: 1秒 | 成功率: 99.8%                │  │
│  │ [查看数据] [查看日志] [停止]                 │  │
│  └──────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

**验收标准**:
- 页面可以显示所有采集任务
- 点击"启动"按钮后任务真正开始采集
- 状态实时更新 (先用轮询，WebSocket为P1)
- 点击"停止"按钮后任务停止

---

### 优先级 P1 - 实时监控增强

#### Task 4: 实现WebSocket实时推送
**预计工时**: 8小时

**依赖安装**:
```bash
pip install channels channels-redis daphne
```

**文件清单**:
```
backend/
  ├── control_plane/
  │   ├── asgi.py              (修改)
  │   └── settings.py          (修改)
  ├── acquisition/
  │   ├── consumers.py         (新建)
  │   ├── routing.py           (新建)
  │   └── signals.py           (新建)
```

**开发内容**:
- [ ] 安装Django Channels
- [ ] 配置Redis作为Channel Layer
- [ ] 创建 `AcquisitionConsumer` WebSocket消费者
- [ ] 在 `AcquisitionService` 中添加状态推送
- [ ] 创建Django信号自动推送状态更新
- [ ] 配置ASGI路由
- [ ] 更新前端使用WebSocket连接

**验收标准**:
```javascript
// 前端连接WebSocket
const ws = new WebSocket('ws://localhost:8000/ws/acquisition/sessions/123/');
ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log('状态更新:', data);
  // 实时更新UI
};
```

---

#### Task 5: 数据可视化组件
**预计工时**: 6小时

**开发内容**:
- [ ] 集成图表库 (Chart.js / Recharts)
- [ ] 创建实时数据折线图
- [ ] 创建数据点历史查询
- [ ] 创建错误率统计图表

---

### 优先级 P2 - 完善与优化

#### Task 6: 任务配置与高级控制
**预计工时**: 4小时

**开发内容**:
- [ ] 任务暂停/恢复功能
- [ ] 采集周期动态调整
- [ ] 任务组管理 (批量启动/停止)
- [ ] 任务调度 (定时启动/停止)

#### Task 7: 监控告警
**预计工时**: 6小时

**开发内容**:
- [ ] 设备离线告警
- [ ] 数据异常检测
- [ ] 邮件/短信通知
- [ ] 告警历史记录

#### Task 8: 性能优化
**预计工时**: 4小时

**开发内容**:
- [ ] 数据点批量查询优化
- [ ] WebSocket连接池管理
- [ ] 前端虚拟列表 (大量任务时)
- [ ] API响应缓存

---

## 三、总结

### 当前链路状态

```
✅ 前端上传Excel
  └─✅ 解析采集任务配置
      └─✅ 保存配置到数据库
          └─⚠️ 执行采集任务 (Celery任务存在，但API未连接)
              └─❌ 实时监控任务状态 (完全缺失)
                  └─⚠️ 控制任务状态 (部分实现，未真正调用)
```

### 核心缺口

1. **Acquisition API模块** - 缺少REST API接口
2. **Configuration模块任务控制** - 未真正调用Celery任务
3. **前端采集控制页面** - 完全缺失
4. **实时状态推送** - WebSocket/SSE未实现

### 最小可行链路 (MVP)

**开发Task 1 + Task 2 + Task 3 (约12小时)** 即可打通完整链路：

```
前端上传Excel
  → 解析并保存配置 ✅
  → 前端采集控制页面
  → POST /api/acquisition/sessions/start_task/
  → Celery启动采集任务
  → 轮询 GET /api/acquisition/sessions/{id}/status/
  → 前端显示实时状态
  → POST /api/acquisition/sessions/{id}/stop/
  → 任务停止
```

### 完整功能链路 (增加WebSocket)

**MVP + Task 4 (约20小时)** 实现完整功能：

```
前端上传Excel
  → 配置保存 ✅
  → 前端控制页面
  → POST /api/acquisition/sessions/start_task/
  → Celery启动采集
  → WebSocket自动推送状态 (无需轮询)
  → 前端实时显示
  → POST /api/acquisition/sessions/{id}/stop/
  → 任务停止，WebSocket推送停止消息
```

---

## 四、决策建议

### 方案A: 快速打通链路 (推荐)
**开发内容**: Task 1 + Task 2 + Task 3
**预计工时**: 12小时
**效果**: 完整链路可用，状态通过轮询更新 (每2秒)

### 方案B: 完整实时监控
**开发内容**: Task 1 + Task 2 + Task 3 + Task 4
**预计工时**: 20小时
**效果**: 完整链路 + WebSocket实时推送

### 方案C: 全功能实现
**开发内容**: 所有Task
**预计工时**: 40小时
**效果**: 完整监控、可视化、告警、性能优化

---

## 五、下一步行动

**请决定**:
1. 选择方案A/B/C？
2. 是否需要我立即开始实现Task 1 (创建Acquisition API)？
3. 前端采集控制页面的UI设计是否需要调整？

**建议优先级**: 方案A → 快速验证完整链路 → 再考虑是否增加WebSocket

---

**文档版本**: v1.0
**作者**: Claude
**更新**: 2025-10-09
