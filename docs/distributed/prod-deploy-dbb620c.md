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

## 3. 待办 — 2 个 prod edge（reduced-config，防二次 wedge）

edge-agent 镜像**未 staged**（off-box 恢复只 build 了 django），且每 edge 自带
`influxdb:2.7`（内存大户）。2× 完整 edge 栈会顶爆余量。按 CEO 预授权 reduced-config：

1. 本地 off-box build `edge-iot-distributed-edge-agent:dbb620c`（`linux/amd64`）→
   `docker save | gzip` → scp → `docker load`（prod 不 build）。
2. **单 influx 共享**两个 edge-agent（override `INFLUXDB_HOST` 指同一实例），
   而非 2× influx。
3. 先起 1 个 edge，盯 `docker stats` + master `:80`，稳了再起第 2 个；
   任一步 master 退化即 abort + 回滚 edge。

### 验收清单（XIU-119）
- [x] master 体系仍 healthy（`:80` → 200）
- [x] distributed center API 可达（`:8002/api/fleet/edges/` → 200）
- [ ] 2 个 prod edge online via MQTT（`mosquitto_sub` 看 lwt online）
- [x] 资源占用 < 70% mem
- [x] 部署日志落仓（本文件）

prod root 凭据见 board 评论（不入仓）。
