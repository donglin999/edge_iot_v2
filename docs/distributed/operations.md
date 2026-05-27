# Distributed 运维 runbook

日常巡检 + 故障排查。配合前端 `/fleet`、`/acquisition`、`/alarms`、`/data`
和 center django 日志、edge-agent 日志使用。

## 日常巡检（每日 1 次）

1. **看 `/fleet` 页**
   - 所有 edge 都 **online**；offline 超 5 分钟的需要查 [edge 离线排查](#edge-离线排查)
   - `buffer_backlog == 0` 或近 0；持续 ≥1000 表示积压
   - `last_backfill_at` 不应反复跳动（频繁断线）
2. **看 `/acquisition` 页**：每个分派到 edge 的 task 应显示 `running`
3. **看 `/alarms` 页**：有 firing 告警的话排到对应工程师 / 现场
4. **抽查 `/data` 页**：选 1–2 个 task 拉近 1 小时历史，Drawer 能正常出图

## 看 fleet 状态（命令行）

```bash
curl -s http://<center>:8000/api/fleet/edges/ | jq '.results[] | {
  name, status, last_seen, last_uplink_seq, buffer_backlog, last_backfill_at
}'
```

字段：

- `status` ∈ {`pending`,`online`,`offline`}；`offline` 由 `sweep_stale_edges` 定时刷
- `last_uplink_seq` 严格单调；下行台阶 = bug
- `buffer_backlog` = edge 端持久 outbox 深度；持续大于 1000 = WS 断了或反压
- `last_backfill_at` = 上次重连补发完成时间

## edge 离线排查

排查清单（自上而下，先简单后复杂）：

1. ssh 上去看 `docker ps` —— edge-agent 容器跑没跑
2. `docker logs edge-agent | tail -50` —— 找 `connect failed` / `reconnecting in Ns`
3. `curl -s http://<center>:8000/api/fleet/edges/` —— 看 last_seen 多旧
4. edge → center 的 8000 端口连通性：在 edge 机上 `curl -v http://<center>:8000/api/fleet/edges/`
5. 看活动 token 是否被吊销（center 删过 EdgeNode 行）：
   ```bash
   docker logs center-django | grep "edge=$EDGE_ID" | grep -i "reject\|invalid_token"
   ```
6. 若 token 错乱 → 在 center 删 + 重发 + 在 edge 重 export EDGE_TOKEN 重启

恢复后的预期：

- 1–2 秒内 `status: online`
- WS 重连后自动 backfill：`last_uplink_seq` 跳到 outbox 的 `seq_high`
- `buffer_backlog` 降到 0

## 积压补发（buffer_backlog 一直涨）

如果 `buffer_backlog` 持续上涨说明 edge 在采但发不出去：

1. 看 edge-agent 日志找 WS 错误：`ws closed` / `connect failed`
2. 检查 center 是否过载：`docker stats center-django`
3. 单条 frame 卡死？查 `apply_config` 帧 backlog —— v0.5 没限速，应不会
4. 紧急：临时扩大 outbox 上限（环境变量 `EDGE_OUTBOX_MAX_DEPTH`，默认 100k）
5. 极端兜底：edge 上 `docker exec edge-agent sqlite3 /var/lib/edge-agent/edge_state.db \
   'DELETE FROM edge_uplink_outbox WHERE seq < N'` —— **会丢数据**，只在确认这段无业务价值时用

## 历史代理 503 排查

前端 `/data` Drawer 报错，或后端 `/api/history/points` 返回 5xx：

| code | 含义 | 处理 |
|------|------|------|
| `edge_offline` | edge 在 fleet 表是 offline | 走 [edge 离线排查](#edge-离线排查) |
| `edge_unreachable` | center 无法 TCP 连到 edge 的 18086 | 检查 `EDGE_HISTORY_URL` label 是否真可达 + 防火墙 |
| `edge_token_missing` | center 没配该 edge 的代理 token | 把 token 加入 `EDGE_HISTORY_PROXY_TOKENS` 重启 center |
| `bad_token` (edge 侧 401) | center 配的 token 跟 edge 期望不一致 | 跟 activation token 对齐 |
| `upstream_5xx` | edge 18086 自身 5xx | ssh edge 看 history_server 日志 |

详细字段定义见 [history-proxy.md](history-proxy.md).

## 告警维护

- 告警规则 CRUD 走 `/api/acquisition/alarm-rules/`，center 自动 push 到所有 online edge
- 一条规则 `device_code` 为空 = 全 edge 全设备同名 point 都判
- 一条规则 `device_code` 填了 = 只判这台设备（M7 双 edge 隔离推荐用法）
- 历史告警 `/alarms` 的 `edge` 列展示来源 edge；center-local 告警此列为空

## 持久化数据备份

- center：`docker compose -f docker-compose.center.yml` 的命名卷 + `backend/db.sqlite3`
  （配置 / 历史 alarm / fleet 表）
- 每台 edge：
  - `edge-influxdb-data` 卷（时序数据，价值最高，按需备份）
  - `edge-agent-state` 卷（`edge_state.db` 含 outbox + seq；丢失会让 seq 重新从 1 开始）

## 红线 / 不要做

- **不要** 在 distributed 模式下给 center 装 InfluxDB —— 设计上中心不存全量
- **不要** 在 center 上跑 `docker-compose.yml`（旧单机栈），会跟 center 的 8000 冲突
- **不要** 直接 SSH 到 edge 改 `/var/lib/edge-agent/edge_state.db` —— 用 outbox 维护脚本
- **不要** 把同一个 `EDGE_TOKEN` 给两台机用 —— `edge_id` 撞库会被 center 拒
