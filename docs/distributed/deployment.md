# Distributed 部署 runbook

从空机到一个 center + N 个 edge 上线的步骤。**装一次 center + 每台 edge 一次。**

## 总览

```
            ┌─────── center (1 台) ───────┐
            │  daphne :8000               │
            │  frontend :5173             │
            │  redis :6379                │
            └────────────┬─────────────────┘
                         │ WS / HTTP
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
    edge-A           edge-B            edge-…
   工控机1           工控机2
   :18086 history HTTP
```

## 前置要求

- Linux 主机（x86_64 或 arm64），Docker 24+ + Docker Compose v2
- center 机：≥2 vCPU / 4 GB RAM / 20 GB 磁盘；出 8000、5173、6379 端口
- 每台 edge 机：≥1 vCPU / 2 GB RAM / 20 GB 磁盘（InfluxDB + outbox 用）
- center → edge 的双向路由：edge 到 center 的 8000，center 到 edge 的 18086
- 时区统一为 Asia/Shanghai（compose 已设）

## Step 1 — 装 center（一次）

```bash
git clone <repo> /opt/edge_iot
cd /opt/edge_iot
git checkout distributed/main
# 编辑 .env / docker-compose.center.yml 设置：
#   EDGE_HISTORY_PROXY_TOKENS  – 每台 edge 的 activation token (JSON 字符串)
#   EDGE_HISTORY_PROXY_URLS    – 可选；若 edge 自己上报 history_url label 就不用配
docker compose -f docker-compose.center.yml up -d --build
```

验证：

```bash
curl -s http://<center>:8000/api/fleet/edges/  # → []
# 浏览器访问 http://<center>:5173/ — 看到前端
```

## Step 2 — 给每台 edge 注册（在 center 做一次/台）

```bash
EDGE_NAME=edge-shanghai-line-1
TOKEN_JSON=$(curl -s -XPOST http://<center>:8000/api/fleet/edges/ \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"$EDGE_NAME\",\"labels\":{\"site\":\"shanghai\",\"line\":\"1\"}}")
echo "$TOKEN_JSON" | jq -r .activation_token   # → 一次性 activation token，记录下来
```

**只能查看一次** —— 这个 token 不会再展示，落地到 edge 后丢了得删 edge 重发。

把它加到 center 的 `EDGE_HISTORY_PROXY_TOKENS` JSON 里并重启 center
（或用 `EDGE_HISTORY_PROXY_DEFAULT_TOKEN` 走单一共享密钥模式）。

## Step 3 — 在每台 edge 工控机部署（每台一次）

```bash
git clone <repo> /opt/edge_iot
cd /opt/edge_iot
git checkout distributed/main

# 关键 env：从 center 拿到的 activation token + center URL + 本机 ID
export EDGE_ID=edge-shanghai-line-1
export EDGE_TOKEN=<step 2 拿到的 activation_token>
export CENTER_URL=ws://<center>:8000/ws/fleet/
export EDGE_LABELS='{"site":"shanghai","line":"1"}'

# history-proxy 让 center 找到本机的可达地址（默认 18086）
# 跨主机部署 EDGE_HISTORY_URL 必须显式设为 center 可访问的 IP/域名
export EDGE_HISTORY_HOST_PORT=18086
export EDGE_HISTORY_URL=http://<edge LAN IP>:18086

docker compose -f docker-compose.edge.yml up -d --build
```

验证（在 center 上看）：

```bash
curl -s http://<center>:8000/api/fleet/edges/ | jq '.results[] | {name,status,labels}'
# {"name":"edge-shanghai-line-1","status":"online","labels":{"site":"shanghai","line":"1","history_url":"http://..."}}
```

## Step 4 — 派任务到该 edge

1. 在前端 `/configuration` 导入 Excel 配置（device + point）
2. 创建 AcqTask；admin 或前端选 `edge` 字段指到该 EdgeNode
3. 让 center 把 apply_config 推下去：

```bash
EDGE_ID=$(curl -s http://<center>:8000/api/fleet/edges/ \
  | jq -r '.results[] | select(.name=="edge-shanghai-line-1").id')
curl -XPOST "http://<center>:8000/api/fleet/edges/$EDGE_ID/assignments/sync/"
```

edge-agent 收到 `apply_config` 后会启动 acquisition pipeline；查看：

```bash
ssh <edge>
docker logs edge-agent | tail -30   # 找 "apply_config v=N: tasks=…"
```

## Step 5 — 验收

按 [m7-acceptance.md](m7-acceptance.md) 的 7 项场景做一次双 edge 验收。

## 滚动升级

- center：拉新代码 → `docker compose -f docker-compose.center.yml up -d --build`
  （django 重启的几秒内 edge WS 会重连 + backfill，业务不丢数据）
- edge：拉新代码 → `docker compose -f docker-compose.edge.yml -p $EDGE_ID up -d --build`
  （edge-agent 重启会从 outbox 续号；acquisition 续跑）

## 卸载

- edge：`docker compose -f docker-compose.edge.yml -p $EDGE_ID down -v`
  + 在 center DELETE `/api/fleet/edges/$EDGE_ID/`
- center：`docker compose -f docker-compose.center.yml down -v`
  + 删 `/opt/edge_iot` 目录

## 常见坑

参见 [operations.md](operations.md#故障排查).
