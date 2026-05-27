# 分布式形态文档

XIU-51 分布式采集系统 (`distributed/main`) 的文档目录页。本系统在单机
（`master`）的基础上拆分出 **center + edge** 两类节点：center 在机房做
控制面 + UI + 历史回查代理，edge 在每条产线/工控机上做本地实时采集 +
本地时序存储 + 本地告警判定。**采集流量不上中心**——edge 把摘要（lifecycle、
1Hz 聚合、告警事件）通过单条 WS 发到 center，原始样本只落地到 edge 本地
InfluxDB，前端 `/data` 看历史时再由 center 代理回查 edge。

## 系统拓扑

```
 ┌───────────── center (1 台) ─────────────┐         ┌──── edge-A (工控机 1) ────┐
 │ daphne + Django                          │ ◀─ WS ─│ edge-agent                 │
 │ fleet (注册/配置下发/告警/历史代理)        │        │ acquisition pipeline       │
 │ Redis (channels)                         │        │ Modbus / OPC UA / S7 …     │
 │ frontend (React /fleet /acquisition …)   │        │ local InfluxDB             │
 │ NO 时序写入                              │        │ durable outbox (sqlite)    │
 └──────────────────────────────────────────┘        │ history HTTP :18086         │
                  ▲                                  └────────────────────────────┘
                  │ HTTP /history/points (按需)
                  │
                  └─────────────────────────── edge-B (工控机 2) …
```

详细一图见 [SYSTEM_ARCHITECTURE.md](../SYSTEM_ARCHITECTURE.md)。

## 概念速查

- **edge / 工控机 / EdgeNode**：装 `docker-compose.edge.yml` 的工控机。
  一个 edge 跑 edge-agent + 本地 InfluxDB + 本地 mock/PLC。注册后由
  center 颁发一次性 activation token。
- **center / 中心**：装 `docker-compose.center.yml` 的机房节点。只跑
  Django + Redis + 前端，**不写 InfluxDB**（在分布式形态下中心没有时序
  存储）。
- **monotonic_seq**：每个 edge 的持久化上行序号，落 edge 端 SQLite outbox。
  支持断线重连后从断点回补，不重不丢。
- **history-proxy**：center 上的只读 HTTP，前端 `/data` 调它，它再代理
  到对应 edge 的 `:18086/history/points`。center 本地没有原始样本。

## 文档索引

按 plan §1–8 顺序：

| 章节 | 文档 | 内容 |
|------|------|------|
| §1 总览 + 部署 | [deployment.md](deployment.md) | center 装一次 + 每台 edge 一次的部署 runbook |
| §1 总览 + 运维 | [operations.md](operations.md) | 日常运维：fleet 状态、离线排查、积压补发、history-proxy 503 |
| §2 协议 (v0.5 STABLE) | [protocol.md](protocol.md) | WS 控制面协议；所有帧 + 字段 + 兼容性规则 |
| §3 M1 fleet / WS skeleton | 已并入 protocol.md | — |
| §3 M2 配置下发 | [m2-smoke.md](m2-smoke.md) | 单 edge 配置下发烟测 runbook |
| §4 M3 lifecycle + 1Hz 聚合 | [m3-smoke.md](m3-smoke.md) | M3 上行烟测 |
| §5 M4 告警同步 | [m4-smoke.md](m4-smoke.md) | M4 告警上行烟测 |
| §6 M5 离线降级 + outbox | [m5-smoke.md](m5-smoke.md) | M5 断线重连 + 持久 seq 烟测 |
| §7 M6 历史回查代理 | [m6-smoke.md](m6-smoke.md) · [history-proxy.md](history-proxy.md) | M6 代理设计 + 烟测 |
| §8 M7 双 edge 真机验收 | [m7-acceptance.md](m7-acceptance.md) | 本期验收报告（M7） |

## 与单机形态的对比

参见仓库根 [README.md](../../README.md#分布式形态简介) 的对比表。
