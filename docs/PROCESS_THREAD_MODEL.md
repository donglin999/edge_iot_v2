# 进程与线程模型

本文描述边缘 IoT 数采系统在运行时的进程 / 线程结构、数据流走向、关键并发原语，以及故障隔离边界。

> 注：本文反映的是当前主线代码（pipeline 重构 + sample_rate_hz + lifecycle event + Celery 双 worker 拆分 + 异步 AlarmSink + Influx 重试 + worker watchdog + orphan 清理之后）的架构。

---

## 1. 进程层（容器维度）

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  Docker Compose                                                              │
│                                                                              │
│  ┌──────────────┐   ┌──────────────────┐   ┌──────────────────┐              │
│  │ django-iot   │   │ celery-acq       │   │ celery-short     │              │
│  │ ASGI server  │   │ Worker (threads) │   │ Worker (threads) │              │
│  │ (Daphne)     │   │ concurrency=200  │   │ concurrency=8    │              │
│  │ :8000        │   │ -Q acquisition   │   │ -Q short         │              │
│  └──────┬───────┘   └─────────┬────────┘   └─────────┬────────┘              │
│         │                     │                      │                       │
│         │   ┌─────────────────┴──────────────────────┘                       │
│         │   │                                                                │
│  ┌──────▼───▼──┐   ┌────────────────┐   ┌──────────────────┐                 │
│  │ redis-iot   │   │ influxdb-iot   │   │ mock-modbus-iot  │                 │
│  │ Channels +  │   │ 时序存储        │   │ 仿真设备 :5020   │                 │
│  │ Celery       │   │ :8086         │   └──────────────────┘                 │
│  │ broker       │   └────────────────┘                                       │
│  │ :6379       │                                                             │
│  └─────────────┘                                                             │
│                                                                              │
│  ┌──────────────┐   ┌──────────────────┐                                     │
│  │ mock-mqtt    │   │ frontend-iot     │                                     │
│  │ Mosquitto    │   │ Vite dev (Node)  │                                     │
│  │ :1883/:9001  │   │ :5173            │                                     │
│  └──────────────┘   └──────────────────┘                                     │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 关键观察

- **django-iot** 用 Daphne (ASGI)，HTTP + WebSocket 共用端口 8000。
- **Celery 已拆分为两个 worker** 按队列分工：
  - `celery-acq` `--pool=threads --concurrency=200 -Q acquisition`：长跑采集任务（`start_acquisition_task` / `stop_acquisition_task`），最多并发 200 个 task。
  - `celery-short` `--pool=threads --concurrency=8 -Q short`：短任务（`acquire_once` / `process_excel_import` / `check_protocol_connection` / `check_storage_connection`），不会被采集任务排队挡住。
  - `task_routes` 在 `control_plane/celery.py` 配置；`task_default_queue = 'short'`。
- I/O bound 的 Modbus / HTTP / WS 调用在 GIL 释放期间允许其它线程跑，threads pool 在多核 CPU 上几乎线性扩展到几百个并发任务。
- 所有有状态的容器都跟 Redis / InfluxDB 解耦，挂掉重启不会丢配置（SQLite 在 django 容器，Influx 数据在 named volume）。

---

## 2. 线程层（celery-acq 进程内，**核心**）

每个采集任务跑在 celery-acq 的一个 worker 线程里，task 内部再展开 pipeline + per-device worker + sinks。多个任务并存时彼此线程隔离，共享同一个 Python 进程：

```
celery-acq 进程
└── threads pool (concurrency=200)
    ├── [Task slot 1] start_acquisition_task(task_id=19)
    │   └── AcquisitionService.run_continuous()  ← service 主循环线程
    │       │
    │       ├─ 5s tick: 写 session.metadata (含 total_points_read, device_health)
    │       ├─ 5s tick: pipeline.dead_workers() → 自动 replace_worker（带 5 次/10 分钟预算）
    │       ├─ 0.5s tick: session.status 轮询，stop 信号触发 pipeline.stop()
    │       │
    │       └── AcquisitionPipeline ──┐
    │            ├── ReadWorker[Dev1]  │ daemon thread
    │            ├── ReadWorker[Dev2]  │ daemon thread, 各持 protocol 连接
    │            └── ...               │ 按 cycle_interval=1/sample_rate_hz 轮询
    │                                  │ read_batch(group) → list[Reading]
    │                                  │ for sink in sinks: sink.consume(r)
    │                                  │ 失败重连 + lifecycle event 状态机
    │                                  │
    │            Sinks（共享，线程安全）│
    │            ├── InfluxDBSink      │ buffer + lock, 50/5s 触发批写
    │            │                     │ 失败指数退避（1→2→4→8→16→30s）
    │            │                     │ buffer maxsize=5000，满时 drop oldest
    │            │                     │ 暴露 total_written 给 service 计数
    │            ├── AlarmSink         │ queue.Queue(1000) + daemon writer 线程
    │            │                     │ 满时 put_nowait drop（每 100 条 1 条 warning）
    │            │                     │ writer 内同步 evaluate + close DB conn
    │            └── WebSocketSink     │ 1Hz Timer flush，按 point_code 去重
    │                                  │ data_point_update 走聚合 buffer
    │                                  │ connection_event 立即 group_send
    │
    ├── [Task slot 2] start_acquisition_task(task_id=20)
    │   └── ... (独立 service + pipeline + workers + sinks 实例)
    │
    └── [Task slot 3..N] ...
```

### 关键并发原语

| 用途 | 工具 |
|---|---|
| 优雅退出信号 | `threading.Event` (`shutdown_event`) — `event.wait(timeout)` 阻塞，set 时立刻唤醒 |
| Sink buffer 并发保护 | `threading.Lock` 内部 |
| Sink 之间 fan-out | 每个 sink 独立处理同一 reading，**不共享 queue** |
| AlarmSink 队列化 | `queue.Queue(maxsize=1000)` + 单 writer 线程，避免规则评估的 SQLite 写锁阻塞 ReadWorker |
| ORM 在线程内使用 | `connection.close()` 在 worker / sink writer 退出 / 长时间操作后调用，防 SQLite 句柄泄漏 |
| Channels group_send | 经 Redis channel layer，跨进程到 django-iot 的 WS consumer |

---

## 3. 线程层（django-iot 进程内）

```
django-iot 进程 (Daphne ASGI)
├── MainThread: ASGI router
├── 每个 HTTP 请求: 1 个 worker thread (DRF 同步视图)
├── 每个 WS 连接: 1 个 asyncio task (Channels async consumer)
│   └── AcquisitionConsumer / GlobalAcquisitionConsumer
│       ├── connect → group_add('acquisition_session_X')
│       ├── 收到 channel layer 消息 → 路由到 handler:
│       │   - data_point_update → 写 socket
│       │   - connection_event → 写 socket
│       │   - session_status_update → 写 socket
│       │   - session_started / stopped → 写 socket
│       └── disconnect → group_discard
├── post_save signal handlers (触发它们的请求线程内同步运行)
│   └── _restart_on_rate_change → 直接改 session.status + Celery apply_async
└── apps.ready() startup hook
    └── 扫 AcquisitionSession.status='running' AND updated_at < now-10min
        → status='error', error_message='Orphan session cleaned up at startup'
```

### Orphan session 清理

celery-acq 容器崩溃时，它正在跑的采集任务的 session.status 在 DB 里会停在 `running`。下次 django-iot / celery-acq 起来时，`apps.ready()` 在 worker 子进程里跑一次 `_cleanup_orphan_sessions()`，把 `updated_at` 老于 10 分钟（远长于 5s 的 metadata 心跳间隔）的 `running` session 全部转成 `error`。

---

## 4. 数据流（一次完整采集 → 看到前端）

```
mock-modbus:5020 (TCP)
    ▲
    │ ① ReadWorker 在 celery-acq 内调 protocol.read_batch(group)
    │    → 1 次 FC03 拿 11 个寄存器（容差合并后的最优计划）
    │
ReadWorker 解码 → list[Reading]
    │
    ├─② sink.consume(r) × N sinks（同步调用，线程安全）
    │
    ├──InfluxDBSink ─→ buffer(maxsize=5000) ─→ 50/5s 触发批写 ─→ InfluxDB :8086
    │   │                                       │
    │   │                                       └─ 失败指数退避 + 数据保留
    │   └─ flush 成功后 _total_written += N，service 主循环每 5s 写到 metadata.total_points_read
    │
    ├──AlarmSink.queue ─→ writer 线程 ─→ 阈值比对 ─→ Alarm 表 (SQLite) + channel layer push
    │
    └──WebSocketSink ─→ buffer ─→ 1Hz Timer flush ─→ channel layer (Redis)
                                                         │
                                                         │③ Redis pub/sub
                                                         ▼
                                            django-iot 进程
                                                Daphne / Channels
                                                AcquisitionConsumer (asyncio)
                                                         │④ websocket.send
                                                         ▼
                                            浏览器 TaskControlPanel
                                            渲染 "📊 收到 N 个测点"
                                            或 "🔄 第 2/3 次重连..."
```

---

## 5. 改采样率的完整路径

```
浏览器 PATCH /api/config/tasks/19/ {sample_rate_hz: 5}
    │
    ▼
django-iot HTTP 线程
    │
    ├─ DRF AcqTaskSerializer.validate
    │   └─ 按 task.points 的 protocols 算最小 PROTOCOL_MAX_HZ
    │       超过即 400（如 modbus_tcp 上限 50Hz）
    │
    ├─ AcqTask.save()
    │     │
    │     ▼ (同进程同线程同步触发)
    │   pre_save signal: 记录 _sample_rate_changed
    │   post_save signal: _restart_on_rate_change
    │       │
    │       ├─ 直接改 session.status = 'stopped' (DB UPDATE)
    │       │
    │       ▼ (跨进程: Redis 队列 → celery-acq 拉新 task)
    │     start_acquisition_task.apply_async(countdown=3, queue='acquisition')
    │       │
    │       ▼ pipeline 检测到 session.status != running
    │         ReadWorker.shutdown_event.set()
    │         所有 worker join → sinks flush → 进程内清理
    │
    ▼
HTTP 200 返回（不等重启）
    │
    ▼ 3 秒后
celery-acq 启动新 task → 新 session_id → 新 pipeline，sample_rate_hz=5.0
ReadWorker 用 auto_timeout = max(0.1, min(配置, cycle_interval × 2))
```

---

## 6. 连接生命周期事件状态机

`ReadWorker` 内部维护一个状态机，事件**只在状态边沿变化**时发射，避免每轮 tick 重复刷屏。

| event | 触发条件 | 频控 |
|---|---|---|
| `connecting` | `_connect()` 调用前（首次或重连周期开始） | 每个断连周期发 1 次 |
| `connected` | `_connect()` 第一次成功 | 一生只发 1 次 |
| `read_failed` | 单次 `read_batch` 抛异常 | 累计 10 次或 5 秒窗口聚合发 1 条 |
| `disconnected` | `now - last_success > connection_timeout` 触发 `protocol.disconnect()` | 1 次 |
| `reconnecting` | `consecutive_failures` 跨阈值进入退避前 | 每次重试前 1 条 |
| `reconnected` | 在 `disconnected` 后 `_connect()` 重新成功 | 1 次（含 downtime_seconds） |
| `gave_up` | `consecutive_failures >= max_reconnect`，进入 5s 退避 | 每次进退避发 1 次 |
| `worker_died` | AcquisitionService supervisor 检测到 worker 线程异常退出 | 由 service 主循环发，每次发现一次 |
| `worker_fatal` | 同 device 10 分钟内重启 ≥5 次，标记为 fatal 不再重启 | 1 次 |

事件经 `WebSocketSink.consume_event(...)` 立即推送（不进 1 Hz 聚合 buffer），前端 `TaskControlPanel` 即时渲染到操作日志。

---

## 7. 故障隔离边界

| 场景 | 影响范围 | 不影响什么 |
|---|---|---|
| Device 1 网络抖动 | ReadWorker[Dev1] 进入 reconnect 循环 | Device 2/3 继续采集；sinks 继续消费别的 device 数据 |
| **单个 ReadWorker 线程崩溃** | service supervisor 5s 内检测到 → replace_worker 自动重启（10 分钟内 ≥5 次后变 fatal） | 同 task 的其它 device、其它任务无感 |
| **InfluxDB 挂了** | InfluxDBSink 进指数退避；buffer 累积到 5000 后开始丢最老 | AlarmSink/WebSocketSink 继续工作；前端仍能看实时值；Influx 恢复后单次补写所有缓冲 |
| Redis 挂了 | channel layer push 失败 → WebSocket 收不到推送；Celery 队列卡 | 当前 pipeline 继续采集；InfluxDB 继续写 |
| 用户改采样率 | 当前 session 1-3s 内被替换 | 历史数据不影响 |
| **celery-acq 容器崩溃** | 全部正在跑的 pipeline 被强杀（daemon=True 跟随进程退出） | systemd / docker `restart: unless-stopped` 拉起新进程；下次启动 `apps.ready()` 自动把 stale running session 转 error |
| celery-acq 死锁 | 影响该容器内所有任务 | celery-short 不受影响（短任务、配置导入仍可用） |

---

## 8. 已知遗留与后续可优化

1. **多任务采同一设备会开多 TCP 连接**：协议层没连接池。如果两个 task 都包含同一台 PLC 的不同点，会建 2 个 socket。设备弱时可能拒连。短期：用户层避免；长期：在 protocols 加 host:port 维度的连接池。
2. **AlarmSink 端到端没大规模验证**：代码路径全在，单点测试通过；但项目里没有规则配置时，writer 线程闲置。
3. **session.metadata 写入仍走 SQLite**：5s 一次，单 worker 没问题；100+ task 同时跑要观察写锁竞争。生产建议切 Postgres。
4. **`tests/test_acquisition_service.py`** 的若干失败用例：post-refactor 期望过时，由测试 agent 持续修复中。

---

## 9. 关键文件索引

| 模块 | 路径 |
|---|---|
| Pipeline 编排 + worker watchdog | `backend/acquisition/services/pipeline.py` |
| Sinks（Influx 重试 / Alarm 异步 / WebSocket 聚合） | `backend/acquisition/services/sinks.py` |
| ReadPlan + Modbus 容差合并 | `backend/acquisition/services/read_plan.py` |
| 协议适配（Modbus / MQTT / OPC UA / S7） | `backend/acquisition/protocols/` |
| 服务主循环 + supervisor + orphan cleanup | `backend/acquisition/services/acquisition_service.py`、`backend/acquisition/apps.py` |
| Celery 任务入口 + task_routes | `backend/acquisition/tasks.py`、`backend/control_plane/celery.py` |
| WebSocket consumers | `backend/acquisition/consumers.py` |
| WS routing | `backend/acquisition/routing.py` |
| 采样率变更 signal | `backend/configuration/signals.py` |
| 采样率字段 + migration | `backend/configuration/models.py:AcqTask`、`migrations/0007_*` |
| Drop schedule 字段 migration | `backend/configuration/migrations/0008_remove_acqtask_schedule.py` |
| Compose 服务定义（含 celery 拆分） | `docker-compose.yml` |
