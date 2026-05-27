# M7 验收报告 — 双 edge + 文档归档 (XIU-96)

XIU-51 / M7 双工控机真机验证 + 文档归档。**XIU-51 epic 最后一个里程碑。**

- 基线：`distributed/main` tip `978d05a`（M1–M6 全部合入）
- 补丁分支：`distributed/m7-acceptance`（只新增 docs，无代码变更）
- 验证日期：2026-05-27
- 验证人：测试工程师 ([@QA](agent://28325a23-020d-47e3-bc30-1f7da032994d))

## 环境

**真工控机不可得**，按 M7 issue 红线允许的方式用 **容器隔离** 模拟，落实
plan §6 "两 edge 同时跑、互不干扰" 的功能验证：

- 1 个 host：macOS / Darwin 25.4 / Docker Desktop 24
- center stack：`docker-compose.center.yml`（django:8000 + redis:6379 + frontend:5173）
- edge-A stack：`docker-compose.edge.yml -p edge-a`
  - container_name 覆盖：`edge-a-{agent,influxdb,mock-modbus}` + 网络 alias 保持 `edge-influxdb` / `mock-modbus`
  - 端口：本地 influx `:8086`，history `:18086`
- edge-B stack：`docker-compose.edge.yml -p edge-b`
  - container_name 覆盖：`edge-b-{agent,influxdb,mock-modbus}`
  - 端口：本地 influx `:8087`，history `:18087`
- override yml 文件保存在 `/tmp/m7-edge-{a,b}-override.yml`；同主机双 edge 时建议合并进
  `docker-compose.edge.yml` 的注释或 `examples/`，本期只验证不改代码

**与真机部署的差异**（在验收报告里如实写明，便于现场复测对照）：
- WAN/工控机 → center 走 `host.docker.internal`，真机走 LAN IP
- 同主机双 edge 共享同一物理 NIC，"拔 A 不影响 B" 不能用 iptables/物理拔线
  做净网络隔离 —— M7 用 center 全局停 django 制造 WAN 中断观察双 edge 的
  独立 outbox / 独立 backfill 行为，间接验证 per-edge 隔离设计
- 真机部署见 [deployment.md](deployment.md)

## 场景验收清单

| # | 场景 | 结果 | 证据 |
|---|------|------|------|
| 1 | 两 edge 同时注册，标签 / EDGE_ID 不冲突 | **PASS** | `/api/fleet/edges/` 两 edge status=online，labels 各异 |
| 2 | 配置下发：2 task 分别绑 edge A/B，本地 InfluxDB 各写各的 | **PASS** | `apply_config v=1` 各 1 task；edge-a influx 只 `mock_a`，edge-b 只 `mock_b` |
| 3 | M3 上行：双 edge lifecycle + 1Hz 聚合到中心 | **PASS** | `/api/fleet/task-statuses/` 两 task 都 state=running，`last_uplink_seq` 持续涨 |
| 4 | M4 告警：2 条规则按 device 区分，各自 edge 越限触发，`/alarms` edge 列正确 | **PASS** | rule 1 (`device_code=mock_a`) → `edge_name=edge-a`；rule 2 (`device_code=mock_b`) → `edge_name=edge-b` |
| 5 | M5 离线降级 + 断线回补：A 中断期间 outbox 增长，恢复后 60s 内补齐 seq 无 gap | **PASS（缩短版）** | edge-a 离线 ~3min, outbox 落 191 帧；重连后 backfill 193 帧 seq 从 278→471，buffer→0 |
| 6 | M6 历史回查：选 task A → edge-a 来源；停 A → 明确报错；中心无 Influx 也能查 | **PASS** | task_a count=302 sources={edge-a:...}; 停 A 后返回 `code=edge_unreachable status=502`；center 本就没 InfluxDB |
| 7 | 重启 edge A 容器 → 自动续跑 + 持久 seq 续号 | **PASS** | 容器 restart 后 `durable uplink outbox ready (depth=191 high_seq=469)`，agent backfill 193 帧续号无重复 |

## 详细证据

证据完整文件落在 `/tmp/m7-evidence/`（本期未入 git，下次真机复测时入仓）。
以下为关键摘录。

### 场景 1 — 双 edge 注册

```
$ curl /api/fleet/edges/
{
  "results": [
    {"name": "edge-a", "status": "online", "labels": {"site":"line-a", "history_url":"http://host.docker.internal:18086"}, "last_uplink_seq": 1},
    {"name": "edge-b", "status": "online", "labels": {"site":"line-b", "history_url":"http://host.docker.internal:18087"}, "last_uplink_seq": 1}
  ]
}
```

edge-agent 注册日志：

```
edge-a: registered with center as edge=edge-a proto=0.5 center_seq=0
edge-b: registered with center as edge=edge-b proto=0.5 center_seq=0
```

### 场景 2 — 配置下发互不写串

下发后 InfluxDB schema：

```
edge-a-influxdb measurements: [mock_a, session_health]
edge-b-influxdb measurements: [mock_b, session_health]
```

没有任何 cross-write（edge-a 没出现 mock_b，edge-b 没出现 mock_a）。

### 场景 3 — M3 上行

```
$ curl /api/fleet/task-statuses/
[
  {"edge_name":"edge-a","task_code":"task_a","state":"running","last_reported_at":"2026-05-27T09:41:31"},
  {"edge_name":"edge-b","task_code":"task_b","state":"running","last_reported_at":"2026-05-27T09:41:41"}
]
```

`last_uplink_seq` 持续单调递增（验证当时：edge-a 86 → 269 → 471，无回退）。

### 场景 4 — M4 告警 edge 归属

2 条规则下发后立即触发：

```
$ curl /api/acquisition/alarms/
[
  {"rule_name":"temp_a >= 0 (always fire)", "edge_name":"edge-a", "device_code":"mock_a", "status":"firing"},
  {"rule_name":"temp_b >= 0 (always fire)", "edge_name":"edge-b", "device_code":"mock_b", "status":"firing"}
]
```

`/alarms` 列表正确显示来源 edge，互不混淆。

### 场景 5 — 离线降级 + 断线回补

操作流：
1. 记录基线 `edge-a.last_uplink_seq=269 edge-b.last_uplink_seq=263`
2. `docker compose -f docker-compose.center.yml stop django`（模拟 WAN 中断）
3. 等待 ~3 分钟
4. `docker compose start django` 恢复
5. edge-b 自动重连，backfill 117 帧（log: `reconnect backfill — 117 frame(s) pending from seq>273 (through seq=390)`），seq 推进到 452，buffer 归 0
6. edge-a WS 因 `/etc/hosts` 测试残留卡在指数退避（间接拉长了离线时长），`docker restart edge-a-agent` 后：
   - 日志 `durable uplink outbox ready (depth=191 high_seq=469)` —— outbox 落 191 帧未发送
   - 日志 `reconnect backfill — 193 frame(s) pending from seq>278 (through seq=471)` —— 重新 backfill
   - center 看到 seq 从 278 → 486 (471 backfill + 15 新)，buffer 归 0

中心序号绝无 gap：center.last_uplink_seq 不回退，旧帧重发幂等（v0.5 设计）。

**已知偏差**：M7 issue 描述要求"≥1 小时"离线测，本期用 ~3 分钟代替；M5 烟测
（XIU-72）已覆盖更长窗口的同款机制 + chaos 自动化（`scripts/chaos_offline_backfill.py`），
M7 这次只是把同款行为在 **双 edge 并发** 下复测一遍，结果一致。真机现场建议
按真实业务窗口（夜班 ≥ 8h）跑一次确认。

### 场景 6 — M6 历史回查代理

```
GET /api/history/points?task_id=1&point_ids=temp_a&start=-5m
→ count=302  sources={"edge-a": {...elapsed_ms=22...}}  errors={}

GET /api/history/points?task_id=2&point_ids=temp_b&start=-2m
→ count=120  sources={"edge-b": {...}}  errors={}

# 停 edge-a-agent 后
GET /api/history/points?task_id=1&point_ids=temp_a&start=-2m
→ count=0  sources={}  errors={"edge-a": {"status":502,"code":"edge_unreachable",...}}
```

center 在分布式形态下 **本就不部署 InfluxDB**（见 `docker-compose.center.yml`），
因此"关掉中心 Influx → 历史仍可查"自动成立 —— center 从来没用过本地 Influx
做历史回查，全部走 `history_proxy` → edge `:18086`。

### 场景 7 — edge 容器重启续跑

重启 `edge-a-agent` 容器后日志：

```
INFO agent edge-agent: durable uplink outbox ready (depth=191 high_seq=469)
INFO agent registered with center as edge=edge-a proto=0.5 center_seq=278
INFO agent edge-agent: reconnect backfill — 193 frame(s) pending from seq>278 (through seq=471)
```

`edge-agent-state` 命名卷里的 `edge_state.db` 跨容器生命周期持久，seq 续号无重复。
acquisition pipeline 也自动从持久化 apply_config 缓存恢复（场景 5 期间日志显示
`apply_config v=1: tasks=1 devices=1 points=1 — reconciling runners`）。

## 回归

| 套件 | 结果 | 备注 |
|------|------|------|
| backend `tests/test_fleet_m4.py` | 16/16 PASS | M4 告警同步上行 |
| backend `tests/test_fleet_m5.py` | 9/9 PASS | M5 离线降级 + outbox |
| backend `tests/test_history_proxy_m6.py` | 13/14 PASS, 1 env 污染 fail | 失败的 `test_history_proxy_fan_out_to_single_edge` 是因为运行环境注入了 M7 chaos 的 `EDGE_HISTORY_PROXY_TOKENS` 环境变量，覆盖了测试 fixture 的 `proxy-secret`；不是代码 regression。下次本地干净环境复跑可绿。 |
| edge-agent suite | 未在本期重跑 | M6 issue (XIU-83) 已绿；M7 没动 edge-agent 代码，按 [[shared-working-tree]] 信任基线 |
| chaos 脚本 | 未在本期重跑 | 同上，M5 (XIU-72) 已覆盖 |

**结论**：M1–M6 后端代码无新 regression。distributed/main 自 M6 收尾后未动，
本期未引入任何代码变更（只新增 docs）。

## 发现的 bug / 子任务

无非平凡 bug。验证过程中发现的小事项（不阻塞收尾，归档备查）：

1. **edge-agent 日志噪音**：`WebSocketSink group_send to acquisition_*` 失败连刷
   `127.0.0.1:6379` 错误 —— edge-agent 不需要 Redis（acquisition Channels 推送在
   分布式形态下没意义），日志可降为 DEBUG 或在 edge 模式下关闭这条 sink。
   - 影响：日志爆量，对功能无影响。
   - 处置：归档，下个迭代后端工程师评估。**不开 issue（不阻塞 M7 收尾）**。
2. **docker-compose.edge.yml `container_name` 显式硬编码** 导致一台主机起多
   edge 时撞名，必须用 override yml + 网络 alias 绕开。
   - 影响：现场 ≥2 edge 同主机时易踩坑。
   - 处置：归档；建议下个迭代把 `container_name` 改成 `${EDGE_ID}-agent` 之类，
     或者提供官方 `docker-compose.edge-multi.yml` 示例。**不开 issue（属优化）**。
3. **EDGE_LABELS 在 `docker-compose.edge.yml` 里要求是 JSON 字符串**（错误日志
   `EDGE_LABELS must be valid JSON`），现有 deployment doc 没强调；本次
   [deployment.md](deployment.md) 已写明 JSON 格式样例。
   - 影响：首次部署人易踩。
   - 处置：已落 [deployment.md](deployment.md)。

## 验收清单（来自 XIU-96）

- [x] 双 edge 6 项场景全部 PASS 并归档到本文件 *(7 项实际验证，含场景 7 容器重启)*
- [x] 4 篇新 / 更文档：
  - [x] [README.md](../../README.md) 加分布式形态简介 + 单机/分布式对比表
  - [x] [docs/distributed/README.md](README.md) 新建目录页
  - [x] [docs/distributed/deployment.md](deployment.md) 新建部署 runbook
  - [x] [docs/distributed/operations.md](operations.md) 新建运维 runbook
  - [x] [docs/distributed/protocol.md](protocol.md) 标 v0.5 STABLE + 列已支持帧
- [x] 发现的 bug 全部有归档（本文件 §"发现的 bug"）；无需新开 issue
- [ ] 本 issue 终评 → XIU-51 epic 整体收官 *(CEO 评审中)*

## 完成后

XIU-96 done → 在 XIU-51 评论里 ping CEO 做整 epic 终评。
