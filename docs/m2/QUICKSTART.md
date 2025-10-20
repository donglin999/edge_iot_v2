# 快速开始指南

## 方案B已实施完成，立即体验完整采集链路！

---

## 一、依赖安装

### 1. 后端依赖

```bash
cd backend

# 安装基础依赖
pip install Django djangorestframework django-environ \
            influxdb-client kafka-python celery redis \
            drf-spectacular pytest pytest-django

# 安装WebSocket支持
pip install channels>=4.0.0 channels-redis>=4.1.0 daphne>=4.0.0
```

### 2. 前端依赖

```bash
cd frontend
npm install
```

---

## 二、启动服务

### 方式A: 简单测试（3个终端）

**终端1 - Django + WebSocket**
```bash
cd backend
python manage.py makemigrations
python manage.py migrate
daphne -b 0.0.0.0 -p 8000 control_plane.asgi:application
```

**终端2 - Celery Worker**
```bash
cd backend
celery -A control_plane worker -l info
```

**终端3 - 前端**
```bash
cd frontend
npm run dev
```

访问: http://localhost:5173

---

## 三、快速验证链路

### Step 1: 上传Excel配置

1. 访问 http://localhost:5173/import
2. 上传测试Excel文件（包含设备和测点配置）
3. 点击"写入配置库"

### Step 2: 启动采集任务

1. 访问 http://localhost:5173/acquisition (新页面!)
2. 看到任务列表
3. 点击"启动"按钮
4. 观察状态实时更新 ✨

### Step 3: 监控采集状态

- 状态变为"running" ✅
- 运行时长实时更新 ✅
- 数据点数量增加 ✅

### Step 4: 停止采集

- 点击"停止"按钮
- 状态变为"stopped" ✅

---

## 四、测试WebSocket推送

打开浏览器Console:

```javascript
// 连接WebSocket
const ws = new WebSocket('ws://localhost:8000/ws/acquisition/global/');

ws.onmessage = (event) => {
  console.log('收到实时推送:', JSON.parse(event.data));
};

// 然后启动任务，查看Console输出
```

---

## 五、API测试

```bash
# 1. 查看所有采集任务
curl http://localhost:8000/api/config/tasks/

# 2. 启动任务 (假设task_id=1)
curl -X POST http://localhost:8000/api/acquisition/sessions/start-task/ \
  -H "Content-Type: application/json" \
  -d '{"task_id": 1}'

# 3. 查看活跃会话
curl http://localhost:8000/api/acquisition/sessions/active/

# 4. 查看会话状态 (假设session_id=1)
curl http://localhost:8000/api/acquisition/sessions/1/status/

# 5. 停止会话
curl -X POST http://localhost:8000/api/acquisition/sessions/1/stop/ \
  -H "Content-Type: application/json" \
  -d '{"reason": "测试完成"}'
```

---

## 六、查看API文档

访问自动生成的Swagger文档:

- Swagger UI: http://localhost:8000/api/docs/
- ReDoc: http://localhost:8000/api/redoc/

---

## 七、运行测试套件

```bash
cd backend
PYTHONPATH=/path/to/backend DJANGO_SETTINGS_MODULE=control_plane.settings \
  python -m pytest tests/ -v

# 预期结果: 43/47 测试通过 (91%)
```

---

## 八、常见问题

### Q: Redis连接失败？
A: 确保Redis已启动: `redis-server`

### Q: WebSocket连接失败？
A:
1. 检查Daphne是否正常运行
2. 确认使用 `ws://` 协议（开发环境）
3. 生产环境使用 `wss://` 协议

### Q: Celery任务不执行？
A:
1. 检查Celery worker是否运行
2. 查看worker日志: `celery -A control_plane worker -l debug`

### Q: 前端显示"获取任务列表失败"？
A:
1. 确认Django服务运行在8000端口
2. 检查 `frontend/vite.config.ts` 的proxy配置

---

## 九、下一步

### 集成WebSocket到前端 (可选)

修改 `AcquisitionControlPage.tsx`:

```typescript
useEffect(() => {
  if (!autoRefresh) return;

  // 使用WebSocket代替轮询
  const ws = new WebSocket('ws://localhost:8000/ws/acquisition/global/');

  ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.type === 'session_status') {
      // 更新会话状态
      setActiveSessions(prev =>
        prev.map(s => s.id === data.data.session_id ? {...s, ...data.data} : s)
      );
    }
  };

  return () => ws.close();
}, [autoRefresh]);
```

### 实施方案C功能

参考 `backend/monitoring/README.md` 开始实现:
1. 设备离线告警
2. 邮件通知
3. 监控大屏

---

## 十、技术支持

遇到问题？

1. 查看完整文档: `docs/m2/plan_b_implementation.md`
2. 查看链路分析: `docs/m2/acquisition_pipeline_gap_analysis.md`
3. 查看测试文档: `backend/tests/README.md`

---

**祝使用愉快！** 🚀

现在你有一个完整的、实时的、可扩展的工业数据采集系统！
