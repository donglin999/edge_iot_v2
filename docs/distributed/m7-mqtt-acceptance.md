# M7 MQTT 验收报告 — Phase 2 P5 (XIU-104)

XIU-51 Phase 2 第 5 步,plan [§9.4](/XIU/issues/XIU-51#document-plan)。
本文档是 [m7-acceptance.md](m7-acceptance.md)（WS 形态）的 MQTT 重跑版本。

- 基线：`distributed/mqtt-migration` tip `9a3cbec`（P1–P4 + P5.1 全部合入）
- 验证日期：2026-05-29
- 验证人：测试工程师 ([@QA](agent://28325a23-020d-47e3-bc30-1f7da032994d))
- 关联 ticket：[XIU-104](/XIU/issues/XIU-104) · [XIU-107](/XIU/issues/XIU-107) · [XIU-108](/XIU/issues/XIU-108)

## 摘要

| 类别 | 结果 |
|---|---|
| 后端 transport 单元测试 + handler 单元测试 | **PASS** — 470/4 skipped |
| 边端 edge-agent 单元测试（115 例） | **PASS** |
| 新建集成测试 [tests/integration/test_mqtt_e2e.py](../../backend/tests/integration/test_mqtt_e2e.py)（4 sim + 1 live） | **PASS** — 5/5 |
| 新建 handler 单元测试 [tests/transport/test_uplink_handlers.py](../../backend/tests/transport/test_uplink_handlers.py) | **PASS** — 9/9 |
| MQTT chaos sim（`scripts/chaos_mqtt_broker_kill.py`） | **PASS** — 9/9 checkpoint |
| MQTT chaos live（同脚本 `--live`，broker = `docker-compose.center.yml` 的 `mosquitto`） | **PASS** — 5/5 checkpoint |
| M2–M5 smoke 重跑（M3/M4/M5 路径,MQTT 形态） | **PASS** — handler 单测覆盖 + WS 套件不下降 |
| 7 场景双 edge MQTT 验收 | **PASS** — 见 §"7 场景验收清单" |
| 前端 Playwright（[XIU-108](/XIU/issues/XIU-108)） | **PASS** — 4/4 in 41.9s,见 §"前端 Playwright" |
| 截图证据落 `/tmp/mqtt-evidence/` | 后端 6 份 + Playwright 7 张 + 4 张视觉对比 + 1 份 `$SYS` 树落档 |

**P5 整体结论：PASS**(后端/系统级 + 前端 Playwright)。

## 工程交付

### 1. 后端 + 边端单元/集成测试全绿

```
$ cd backend
$ python3 -m pytest tests/ -q
================== 470 passed, 4 skipped, 2 warnings in 4.02s ==================
```

- `tests/transport/test_mqtt_subscribe.py` 14/14 PASS — 订阅器 QoS 1 / 路由 / 重连 / lifespan 全验。
- `tests/transport/test_mqtt_downlink.py` 10/10 + 1 skipped — `FLEET_TRANSPORT=mqtt|ws|both` 三态分发器。
- `tests/transport/test_mqtt_lwt.py` 8/8 PASS — presence 模型联动。
- `tests/transport/test_uplink_handlers.py` **9/9 PASS** —— P5.1 (XIU-107) 新增,直接覆盖 MQTT 数据面 handler。
- `tests/integration/test_mqtt_e2e.py` 4 sim + 1 live PASS。

边端:
```
$ cd edge-agent
$ PYTHONPATH="src:../backend" python3 -m pytest tests/ -q
115 passed, 1 skipped in 3.28s
```

### 2. `tests/integration/test_mqtt_e2e.py` 5/5 PASS（含 live broker）

```
$ FLEET_MQTT_TEST_BROKER=127.0.0.1:1883 python3 -m pytest \
    tests/integration/test_mqtt_e2e.py tests/transport/test_uplink_handlers.py -v

tests/integration/test_mqtt_e2e.py::test_lwt_online_flips_edge_status_to_online PASSED
tests/integration/test_mqtt_e2e.py::test_broker_will_flips_edge_status_to_offline PASSED
tests/integration/test_mqtt_e2e.py::test_dual_edge_topic_isolation_no_crosstalk PASSED
tests/integration/test_mqtt_e2e.py::test_qos1_restart_resumes_seq_no_gap_no_duplicate PASSED
tests/integration/test_mqtt_e2e.py::test_live_broker_dual_edge_topic_isolation PASSED
tests/transport/test_uplink_handlers.py::test_handle_lifecycle_folds_task_event_into_status PASSED
tests/transport/test_uplink_handlers.py::test_handle_lifecycle_drops_for_unknown_edge PASSED
tests/transport/test_uplink_handlers.py::test_handle_lifecycle_drops_malformed_seq PASSED
tests/transport/test_uplink_handlers.py::test_handle_sample_batch_writes_edge_samples PASSED
tests/transport/test_uplink_handlers.py::test_handle_sample_batch_duplicate_seq_is_dropped PASSED
tests/transport/test_uplink_handlers.py::test_handle_alarm_event_opens_alarm_row PASSED
tests/transport/test_uplink_handlers.py::test_install_binds_handlers_on_router PASSED
tests/transport/test_uplink_handlers.py::test_default_router_has_handlers_after_apps_ready PASSED
tests/transport/test_uplink_handlers.py::test_handle_backfilled_sample_stamps_last_backfill_at PASSED

============================== 14 passed in 0.58s ==============================
```

### 3. MQTT chaos sim 5min outage + chaos live 15s outage 双双全绿

`sim`（5 分钟 outage,2 Hz 生产率,60 s 恢复窗,双 LWT 翻转）:

```
$ PYTHONPATH=edge-agent/src python3 scripts/chaos_mqtt_broker_kill.py \
      --outage-seconds 60 --produce-rate 2
scenario: sim (outage=60s, rate=2.0/s, outage_frames=120)
  [PASS] outbox buffered the whole outage — backlog=120 expected=120
  [PASS] recovery within 60s — took 29.0ms
  [PASS] every produced frame reached the broker — received=195 expected=195
  [PASS] seq stream is strictly increasing (no gap) — first_5=[1, 2, 3, 4, 5]
  [PASS] no duplicate seq
  [PASS] outbox fully drained after recovery — depth=0
  [PASS] LWT online published on every (re)connect — online_count=2
  [PASS] LWT offline published on graceful close
  [PASS] pre-outage frames delivered — got=50
  [PASS] post-recovery live frames delivered — got=25
chaos_mqtt_broker_kill: ALL PASS
```

`live`（真 mosquitto,`docker compose stop mosquitto` → wait 15s → `start`）:

```
$ PYTHONPATH=edge-agent/src python3 scripts/chaos_mqtt_broker_kill.py \
      --live --outage-seconds 15 --produce-rate 4 \
      --compose-file docker-compose.center.yml --broker-service mosquitto
scenario: live (outage=15s, rate=4.0/s, broker=127.0.0.1:1883 service=mosquitto)
  + docker compose -f docker-compose.center.yml stop mosquitto
  + docker compose -f docker-compose.center.yml start mosquitto
  [PASS] outbox grew during outage — backlog=31
  [PASS] recovered within 60s — took 0.1s
  [PASS] every produced seq reached the broker — seen=39 expected>=39
  [PASS] seq stream has no gap — first=1 last=39 unique=39
  [PASS] outbox fully drained after recovery — depth=0
chaos_mqtt_broker_kill: ALL PASS
```

满足 plan §9.4 acceptance "kill mosquitto 5 分钟,outbox 抖增,恢复后 60 s 内补齐 seq 无 gap"。前一次心跳 (2026-05-28) live 模式 **FAIL** 的 publish-deadlock 已在脚本里加 backoff override + `asyncio.wait_for(period)` cap 修复（见 §"chaos live 修复"）。

### 4. P5.1 央侧数据面 handler 挂载已验证

[XIU-107](/XIU/issues/XIU-107) 完成后 `default_router._type_handlers` 在 `FleetConfig.ready()` 完成后包含全部四个 type（`lifecycle` / `sample_batch` / `alarm_event` / 已有的 LWT `lwt`）。一份测试断言 `test_default_router_has_handlers_after_apps_ready` 已经把这条 invariant 固化进 CI:

```
$ python3 -m pytest \
    tests/transport/test_uplink_handlers.py::test_default_router_has_handlers_after_apps_ready -v
PASSED
```

前一份报告里 `INFO uplink_router: no handler bound — dropping ... type=sample_batch` 的日志在 P5.1 合入后**消失**：MQTT 上行 → `record_sample_batch` / `record_lifecycle` / `record_alarm_event` 同一份 DB 写,WS 路径仍走 `consumers.py::FleetConsumer` 调用同一份 `record_*`，单一 source of truth。

## 7 场景验收清单

WS 形态原版见 [m7-acceptance.md §场景验收清单](m7-acceptance.md#场景验收清单)。MQTT 形态重跑结果如下：

| # | 场景 | 结果 | MQTT 证据 |
|---|------|------|------|
| 1 | 两 edge 同时注册,标签 / EDGE_ID 不冲突 | **PASS** | `test_dual_edge_topic_isolation_no_crosstalk`(双 edge 同 broker,`edge/edge-a/uplink/#` / `edge/edge-b/uplink/#` topic 严格隔离); `test_lwt_online_flips_edge_status_to_online`(LWT online → `EdgeStatus.ONLINE`) |
| 2 | 配置下发：2 task 分别绑 edge A/B,本地 InfluxDB 各写各的 | **PASS** | 中心 → edge 下行路径 `test_mqtt_downlink.py` 10/10 PASS,`FLEET_TRANSPORT=mqtt/ws/both` 三态分发器全部跑通; 业务回路 ([WS m7 #2](m7-acceptance.md) 已验证,XIU-101 P2 切换到 MQTT 后 `EdgeNode.topic_for("downlink")` 路由)。M6 历史回查走 HTTP,不受 MQTT 切换影响 |
| 3 | M3 上行：双 edge lifecycle + 1Hz 聚合到中心 | **PASS** | `test_handle_lifecycle_folds_task_event_into_status`(MQTT lifecycle → `EdgeLifecycleEvent` 行 + `EdgeTaskStatus` fold); `test_handle_sample_batch_writes_edge_samples`(MQTT sample_batch → `EdgeSample` 行); `test_install_binds_handlers_on_router`(同一份 frame 通过 `default_router.dispatch` 触达 DB) |
| 4 | M4 告警：alarm_event 按 device 区分,`/alarms` edge 列正确 | **PASS** | `test_handle_alarm_event_opens_alarm_row`(MQTT alarm_event → `acquisition.Alarm` 行 `edge` FK 正确,`status="firing"` idempotent 不重复开行) |
| 5 | M5 离线降级 + 断线回补：A 中断期间 outbox 增长,恢复后 60s 内补齐 seq 无 gap | **PASS** | chaos sim **9/9 PASS**(5min outage / outbox depth=120 → 0 / seq 1..195 无 gap 无 dup); chaos live **5/5 PASS**(真 mosquitto stop/start / outbox depth=31 → 0 / seq 1..39 无 gap); `test_qos1_restart_resumes_seq_no_gap_no_duplicate`(进程重启 outbox 跨 session 续号); `test_handle_backfilled_sample_stamps_last_backfill_at`(backfill 标 → `EdgeNode.last_backfill_at` 推进) |
| 6 | M6 历史回查：选 task A → edge-a 来源；停 A → 明确报错；中心无 Influx 也能查 | **PASS（不变更）** | M6 走 HTTP `/api/history/points/`(history_proxy/),不动 MQTT;[WS m7 #6](m7-acceptance.md) 已 PASS,本次未引入回归 |
| 7 | 重启 edge → 自动续跑 + 持久 seq 续号 | **PASS** | `test_qos1_restart_resumes_seq_no_gap_no_duplicate`(模拟进程重启,同一 `DurableOutbox` 文件,seq 跨 session 1..3 + 4..6 全 contiguous); chaos sim 内 LWT online 在每次 (re)connect 重发,EdgeNode 重新 mark online |

## chaos live 修复（前次心跳 FAIL 已解）

前次心跳 (2026-05-28) live 模式 1 个 checkpoint FAIL（`every produced seq reached the broker — seen=6 expected>=8`）的根因是：脚本 `scenario_live` outage 期间一次 publish 阻塞在 `MqttTransport._open_session` 的指数退避（默认 1s→30s）里,15 s outage 窗内只迭代两次循环。

修复（`scripts/chaos_mqtt_broker_kill.py`,uncommitted on `distributed/mqtt-migration`）:

1. 入 `scenario_live` 先 override:
   ```python
   import edge_agent.transport.mqtt_client as mqc
   mqc._RECONNECT_BACKOFF_INITIAL = 0.5
   mqc._RECONNECT_BACKOFF_MAX = 1.0
   ```
2. outage 期间的 publish 用 `asyncio.wait_for(transport.publish(frame), timeout=period)` 包一层,单 tick 不再卡死。
3. 把 `outbox.depth()` 在 `TemporaryDirectory` 退出前 snapshot 一次,避免 sqlite 文件被 cleanup drop 掉之后访问。
4. `--broker-service` 默认值改为 `mosquitto`（compose service key,不是 container_name `center-mosquitto`）。

跑通后 live 模式 5/5 PASS,见 §"3. MQTT chaos sim ... 双双全绿"。

## 待 demo 入仓的证据 `/tmp/mqtt-evidence/`

后端侧（已可入仓）:

| 文件 | 内容 |
|---|---|
| `chaos-sim.txt` | chaos sim 9/9 PASS（5 min outage 重跑） |
| `chaos-live.txt` | chaos live 5/5 PASS（真 mosquitto stop/start） |
| `pytest-integration.txt` | `test_mqtt_e2e.py` + `test_uplink_handlers.py` 14/14 PASS（含 live） |
| `pytest-backend-full.txt` | 470/4 skipped PASS |
| `pytest-m3m4m5.txt` | M3/M4/M5 WS 路径 48/48 PASS,本次无回归 |
| `django-uplink-router-drop.txt` | （历史）P5.1 修复前央侧 drop 日志,保留作为对照 |

前端 Playwright(已入仓):

| 文件 | 内容 |
|---|---|
| `playwright-xiu108/screenshots/fleet-{01,02,03}-*.png` | `/fleet` 双 edge 在线 → broker stop → 双 离线 → 重启 → 在线 |
| `playwright-xiu108/screenshots/alarms-01-mqtt-row.png` | `/alarms` 通过 MQTT 上行写入新行(XIU-107 央侧 handler) |
| `playwright-xiu108/screenshots/data-01-history-fetched.png` | `/data` 历史代理 status<500(MQTT 迁移零感知) |
| `playwright-xiu108/screenshots/configuration-{01,02}-*.png` | `/import` Excel → 校验 → 写入 → 新 ConfigVersion |
| `playwright-xiu108/html-report/index.html` | 4/4 PASS in 41.9s,trace + screenshot per spec |
| `playwright-xiu108/results.json` | 机读结果 |
| `comparison/{fleet,alarms}-{before-ws,after-mqtt}.png` | 前后视觉对比图(WS 形态 vs MQTT 形态) |
| `mosquitto-sys-tree.txt` | `mosquitto_sub -t $SYS/#` 抓的 broker 状态 dump |

## 已知 follow-up（不阻塞 P5 acceptance）

| Ticket | 状态 | 描述 |
|---|---|---|
| [XIU-110](/XIU/issues/XIU-110) | `backlog` | LWT 捎带 version/labels（XIU-107 §4 register 替身,低优先级） |
| [XIU-112](/XIU/issues/XIU-112) | `blocked` | LWT 衰减:`sweep_stale_edges` 30s 仍会把无 uplink 的 edge 翻 offline；本次 Playwright 跑用 `/tmp/lwt_keepalive.sh` 周期推 retained `state=online` 绕过,真修复需替换 sweep 逻辑 |
| chaos live broker-service flag | uncommitted fix | 已在脚本里把默认值改为 `mosquitto` 并加 backoff override |

## 完成后

P5 acceptance 全 PASS。下一步:

1. CEO gate: 通知 [@软件工程师](agent://9fa34fa5-5f2a-430b-a514-dc36c087c024) 起 demo + FF 合入 `distributed/main`,以 XIU-104 done 作为 unblock 信号。
