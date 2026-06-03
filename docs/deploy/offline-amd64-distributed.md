# 离线 amd64 部署 runbook —— 分布式(distributed/main)

无网络工控机(amd64)上部署分布式 edge_iot_v2:1 台 center + N 台 edge,边-央
uplink 走 MQTT(Phase 2,含已合并的 lwt 修复 [XIU-112](/XIU/issues/XIU-112))。

相关：[XIU-128](/XIU/issues/XIU-128) · [XIU-119](/XIU/issues/XIU-119) prod 部署 ·
`docs/distributed/prod-deploy-dbb620c.md` · 经验 [[prod-deployment]] [[frontend-build-on-host]]

> ⚠️ **构建前置**:分布式套件必须打**合并后的 `distributed/main`**(含 lwt 修复),
> 即 [XIU-126](/XIU/issues/XIU-126) 合并回归通过之后再 build。在合并完成前本套件不要产出。

---

## 0. 拓扑与端口

```
   [center 工控机]  nginx :8002 (SPA + /api,/ws→django) · mosquitto :1883
        ▲   MQTT uplink (edge→center)  +  WS 注册/控制
        │
   [edge-a 工控机] edge-agent + 本地 influxdb · history :18088
   [edge-b 工控机] edge-agent + 本地 influxdb · history :18089
```

| 组件 | 对外端口 |
| --- | --- |
| center web(nginx) | **:8002** |
| center mosquitto | **:1883** |
| edge-a history | **:18088** |
| edge-b history | **:18089** |

镜像清单(全部 amd64):
- center:`edge-iot/backend:offline-amd64`、`edge-iot/web:offline-amd64`、`redis:7-alpine`、`eclipse-mosquitto:2`
- edge:`edge-iot/edge-agent:offline-amd64`、`influxdb:2.7`、`python:3.10-slim`(可选 smoke)

---

## 1. 构建机:产出两个子套件(联网)

```bash
cd ~/project/edge_iot_v2
git checkout distributed/main && git pull        # 必须含 XIU-126 合并 tip
cd frontend && npm ci && npm run build && cd ..   # host 构建 SPA 静态包

bash scripts/offline/build-distributed-amd64.sh
```

产物 `dist/offline/distributed-amd64/`(**不入 git**):

```
center/  images.tar(+sha256,+manifest) docker-compose.yml load-and-up.sh mosquitto/
edge/    images.tar(+sha256,+manifest) docker-compose.yml edge.env.example load-and-up.sh mock/
```

`center/` 拷到 center 工控机,`edge/` 拷到每台 edge 工控机。

---

## 2. 工控机最低要求

- **center**:内存 ≥ 1.5 GiB(无 influx/采集,较轻)。
- **edge**:内存 ≥ 2 GiB(本地 `influxdb:2.7` 是大头)。低配机用
  **reduced-config**(零 influx,off-box load):见 §4——这是 prod 1.7 GiB 机器
  用过的减配路线([XIU-119](/XIU/issues/XIU-119))。
- 磁盘 ≥ 10 GiB;Docker 24+ / compose v2;架构 amd64。
- **严禁工控机本机 `--build`**(prod 被 pip OOM thrash 打爆,见 [[prod-deployment]])。

---

## 3. 部署顺序

### 3.1 起 center

```bash
cp -r /media/usb/center /opt/edge_center && cd /opt/edge_center
./load-and-up.sh
curl -fsS -o /dev/null -w '%{http_code}\n' http://localhost:8002/   # 期望 200
# mosquitto 自检:
docker exec center-mosquitto mosquitto_sub -h 127.0.0.1 -p 1883 -t '$SYS/#' -C 1 -W 3
```

### 3.2 注册 edge(在 center 上拿 token)

```bash
# <edge-id> 自定义,如 edge-shanghai-line-1
curl -s -X POST http://localhost:8002/api/fleet/edges/ \
  -H 'Content-Type: application/json' \
  -d '{"edge_id":"edge-shanghai-line-1","labels":["line-1"]}'
# 响应里的一次性 token 填进该 edge 的 edge.env (EDGE_TOKEN)
```

### 3.3 起 edge(每台)

```bash
cp -r /media/usb/edge /opt/edge_agent && cd /opt/edge_agent
cp edge.env.example edge.env
#   编辑 edge.env:EDGE_ID / EDGE_TOKEN / CENTER_URL=ws://<center-ip>:8002/ws/fleet/
#   EDGE_MQTT_BROKER=mqtt://<center-ip>:1883 / EDGE_HISTORY_HOST_PORT=18088(或 18089)
./load-and-up.sh --env-file edge.env
```

---

## 4. reduced-config(低内存 edge:零 influx,off-box load)

参考 [XIU-119](/XIU/issues/XIU-119):edge 不跑本地 influx,采集样本由 center 按需
经 history 口(:180xx)off-box 拉取。

1. 在 `edge/docker-compose.yml` 注释掉整个 `influxdb` 服务及 `edge-agent` 的
   `depends_on: influxdb`。
2. `edge.env` 设 `EDGE_INFLUX_ENABLED=false`。
3. `./load-and-up.sh --env-file edge.env` —— edge-agent 只保留采集 + MQTT uplink +
   history server,内存占用显著下降。

---

## 5. 健康校验(重点:lwt 不误判)

```bash
# center 看 fleet:双 edge 应 online
curl -s http://localhost:8002/api/fleet/edges/ | python3 -m json.tool
```

**关键回归断言(XIU-112 合并修复)**:edge 静置(无新帧)≥30s 后状态**仍 online**,
不再因 idle 被 `sweep_stale_edges` 误判 offline;只有真正断连(MQTT LWT 触发)才转
offline。若静置即掉线 → 说明套件没打到含 lwt 修复的 `distributed/main`,回退构建。

---

## 6. 与单体 / master 共存红线

- 分布式 center 用 **:8002**,edge 用 **:18088/:18089**,与单体 :80 完全错开。
- center mosquitto :1883 若与单体 smoke mosquitto 同机,务必其一改口或分机。
- master 体系容器名 `*-iot`、分布式 `center-*`/`edge-*`,无重名;不触碰 master
  的 compose/进程/db([[prod-deployment]])。

---

## 7. 关停与回滚

```bash
# edge:
cd /opt/edge_agent && docker compose --env-file edge.env -f docker-compose.yml down
# center:
cd /opt/edge_center && docker compose -f docker-compose.yml down
```

按 [[shutdown-after-dev-test]] 跑完关停所有本地进程/容器。
