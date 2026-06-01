# Prod 部署日志 — distributed/main@dbb620c（XIU-119，与 master 共存）

目标：把 Phase 2（MQTT 迁移）完整体系 `distributed/main@dbb620c` 部署到 prod
`146.56.195.88`，**与现网 master 体系共存、绝不影响 master**。

- 路径：`/opt/edge_iot_distributed/`（独立于 master `/opt/edge_iot/app`）
- 端口：center django **8002**、frontend **5175**、mosquitto **1884**、edge influx **8088/8089**、edge history **18088/18089**
- compose 工程名：`edge_iot_distributed`

---

## 1. 2026-05-31 — 二次教训：绝不在 prod 小 VM 上 build（诱发 Sev1）

prod VM = **1.7 GiB RAM / 无 swap**。在该机上跑
`docker compose ... up -d --build`（pip 装 numpy/pandas/daphne）→ BuildKit 把内存
打满 → swap-thrash 卡死整机：sshd 无法完成 KEX（`Connection reset by peer`），
master（:80）随之宕机 ~数小时。无 in-band 恢复路径。

**根因 + 永久护栏：**

- ❌ 永远不要在这台 VM 上 `--build` / 跑 pip。
- ✅ 镜像一律 **off-box build（`linux/amd64`）→ `docker save | gzip` → scp →
  `docker load`**，prod 只 load + run。
- ✅ center-django 加 `mem_limit`，限制单容器爆炸半径，OOM 只杀自己不波及 master。
- ✅ 每一步前后复验 `curl http://146.56.195.88/ → 200`；master 退化即 abort + 回滚。

**解锁动作（只有人能做）：** 从 Oracle Cloud 控制台**硬重启** VM（温重启清临时
swap、杀僵尸 build，master 容器按 restart policy 自启）。

## 2. 2026-06-01 — 重启后恢复 + center 栈上线（Sev1 解除）

### master 自愈确认（#1 护栏）
- box 重启，uptime 重置，SSH 恢复（KEX 正常），`fstab` 无 swap（临时 swap 已清）。
- `curl http://146.56.195.88/ → 200`；master 全部容器 `Up (healthy)`
  （django-iot / celery-acq / celery-short / influxdb-iot / redis-iot / mock-* /
  frontend-iot）。master 未受任何部署操作影响。

### distributed center 栈（off-box 镜像，prod 零 build）
1. 本地 cross-build `linux/amd64`：`edge-iot-distributed-django:dbb620c`，
   `docker save | gzip` → `/tmp/django-amd64-dbb620c.tar.gz`（195 MB）。
2. scp → prod `/tmp/` → `docker load`（镜像 584 MB on disk）。
3. django-only override `/tmp/center-django-prod.yml`：
   ```yaml
   services:
     django:
       image: edge-iot-distributed-django:dbb620c
       mem_limit: 480m
       environment:
         - DEBUG=0
         - ALLOWED_HOSTS=146.56.195.88,localhost,127.0.0.1,django,center-django,0.0.0.0
   ```
4. 起栈（**`--no-build`**，复用 load 的镜像，不动已 healthy 的 redis/mosquitto）：
   ```bash
   cd /opt/edge_iot_distributed
   docker compose -p edge_iot_distributed \
     -f docker-compose.center.yml -f /tmp/center-django-prod.yml \
     up -d --no-build django
   ```

### 结果
- `center-redis` / `center-mosquitto`：重启后随 restart policy 自启，healthy。
- `center-django`：migrate 全绿（含 Phase 2 `fleet.0004_m5_offline`），daphne 监听
  8000，**MQTT subscriber 已连 `center-mosquitto:1883`**，订阅
  `edge/+/uplink/#` + `edge/+/lwt`。
- **center API `:8002/api/fleet/edges/` → HTTP 200。**
- 资源：mem used ≈ **55%**（952/1720 MiB），available 586 MiB，无 swap，
  center-django 远在 480m cap 之下。每步复验 master `:80` 始终 200。

## 3. 2026-06-01 — 2 个 prod edge 上线（reduced-config，零 influx）

edge-agent 镜像 off-box build（`linux/amd64`）→ `docker save | gzip`
（`/tmp/edge-agent-amd64-dbb620c.tar.gz`，123 MB）→ scp → `docker load`，
**prod 零 build**。

**关键决策 — 不起 influx**：fresh center 无 acq task → edge 不写样本 → influx
非必需。每 influx ~150–250 MB，省下来正好避开二次 wedge（部署后 mem ≈ 70%）。
edge-agent 实测无 influx 也干净启动(`INFLUXDB_HOST` 指 host.docker.internal，
连不上但因无写入不报错)。

**compose**：`/opt/edge_iot_distributed/docker-compose.edge.reduced.yml`
（project `edge_iot_dist_edges`）。要点：

- `image: edge-iot-distributed-edge-agent:dbb620c`（load 的，无 build）
- `mem_limit: 300m`/容器（限爆炸半径）
- `extra_hosts: ["host.docker.internal:host-gateway"]` → edge 容器经
  `host.docker.internal:1884` 连 center-mosquitto、`:8002` 连 center
  （走 host 已发布端口,无需跨 compose 网络）
- `EDGE_ID` = edge **name**（`prod-edge-a`/`-b`,见 `consumers.py:208`
  `name = frame.get("edge_id")`),`EDGE_TOKEN` = 注册返回的 `activation_token`
- `EDGE_TRANSPORT=mqtt`,history 端口 18088/18089(host)→18086(容器)

**注册**：`POST :8002/api/fleet/edges/ {"name":"prod-edge-a",...}` → id=1 + token；
prod-edge-b → id=2 + token。先起 edge-a,验稳(mem/master),再起 edge-b。

**结果**：
- 两 edge MQTT lwt online,`docker exec center-mosquitto mosquitto_sub -t edge/+/lwt`
  看到 `edge/prod-edge-a/lwt {"state":"online"}` + `edge/prod-edge-b/lwt {...online}`
- 日志均 `registered with center as edge=prod-edge-{a,b} proto=0.5`
- mem：edge-a 92 MB / edge-b 77 MB / center-django 128 MB;系统 used ≈ **70%**
  （available 338 MB + 394 MB 可回收 cache,无 swap,master 全程 200）

### ⚠️ XIU-112 注意（给 QA）
deployed `views.py:42` `list()` 调 `sweep_stale_edges()` —— **dbb620c 未含
XIU-112 修复**。idle edge 无 uplink → `last_seen` 老化 → `GET /api/fleet/edges/`
**显示 offline**(即使 MQTT lwt=online)。这是中心侧已知 bug([XIU-112]),非部署
失败。真值以 broker lwt 为准。QA `/data` 页若显示 edge offline,先确认是否
XIU-112 老化所致(可临时缩短 sweep 窗或给 edge 发一次 uplink)。

### 验收清单（XIU-119）
- [x] master 体系仍 healthy（`:80` → 200,全程复验）
- [x] distributed center API 可达（`:8002/api/fleet/edges/` → 200）
- [x] 2 个 prod edge online via MQTT（`mosquitto_sub` 看到双 lwt online）
- [~] 资源占用 ≈ 70% mem（边界值;338 MB avail + 394 MB cache,稳定无 swap;
      正因如此**没起 influx**,也不应再加服务）
- [x] 部署日志落仓（本文件）

### 运维 / teardown
- 起 edges：`cd /opt/edge_iot_distributed && docker compose -f docker-compose.edge.reduced.yml up -d`
- 停 edges：`docker compose -f docker-compose.edge.reduced.yml down`
- center 栈：project `edge_iot_distributed`,`docker-compose.center.yml` +
  `/tmp/center-django-prod.yml`（django-only override）
- **绝不在本 VM `--build`**;镜像永远 off-box load。

prod root 凭据见 board 评论（不入仓）。
