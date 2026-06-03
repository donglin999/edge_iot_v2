# 离线 amd64 部署 runbook —— 单体(master)

无网络工控机(linux/amd64)上部署单体 edge_iot_v2。目标机**不能** `docker pull` /
`pip install` / `npm i`,所有镜像随 U 盘以 `images.tar` 形式带入。

相关：[XIU-128](/XIU/issues/XIU-128) · 经验 [[prod-deployment]] · [[frontend-build-on-host]]

---

## 0. 角色与产物

| 阶段 | 机器 | 动作 |
| --- | --- | --- |
| 构建 | 联网构建机(Mac/arm64 可) | `npm run build` + `buildx --platform linux/amd64` + `docker save` |
| 拷贝 | U 盘 / 移动硬盘 | 把 `dist/offline/monolith-amd64/` 整目录拷过去 |
| 部署 | 离线工控机(amd64) | `docker load` + `docker compose up` |

镜像清单(6 个,全部 amd64):
`edge-iot/backend:offline-amd64`、`edge-iot/web:offline-amd64`、
`redis:7-alpine`、`influxdb:2.7`、`eclipse-mosquitto:2`、`python:3.10-slim`。

> SPA 前端**不**用 node 容器跑 `npm run dev`(离线起不来);改为构建期 host 上
> `vite build` 出静态包,烤进 `edge-iot/web`(nginx)镜像,运行期 nginx :80 提供
> 页面并反代 `/api`、`/ws` → django:8000。

---

## 1. 构建机:产出安装套件(联网)

```bash
cd ~/project/edge_iot_v2
git checkout master && git pull

# 1) host 上构建前端静态包(容器内跨架构会炸,见 frontend-build-on-host)
cd frontend && npm ci && npm run build && cd ..   # 产出 frontend/dist/

# 2) 一键 build amd64 镜像 + docker save 进 tar
bash scripts/offline/build-monolith-amd64.sh
```

产物在 `dist/offline/monolith-amd64/`(**不入 git**,见 `.gitignore`):

```
images.tar            所有 6 个镜像
images.tar.sha256     校验和
manifest.txt          镜像清单 + arch=amd64 + 大小
docker-compose.yml    离线 compose(已 load 的 tag,无 build)
load-and-up.sh        目标机一键加载脚本
mock/                 可选 smoke 用 mock 文件
```

把整个目录拷到 U 盘。

---

## 2. 工控机最低要求

- **内存 ≥ 2 GiB**(单体 8 容器运行态实测约 1.2~1.5 GiB)。prod 教训:1.7 GiB
  无 swap 的小机器跑满配会 thrash;若 < 2 GiB,**加 4 GB swapfile** 兜底
  (`fallocate -l 4G /swapfile && mkswap /swapfile && swapon /swapfile`)。
- **磁盘 ≥ 10 GiB** 空闲(镜像 + influx 数据)。
- Docker Engine 24+ 与 compose v2(目标机需预装;离线安装 docker 自身不在本套件范围)。
- 架构必须 **x86_64/amd64**。

---

## 3. 工控机:加载并起栈(离线)

```bash
# U 盘挂载后,拷到本地
cp -r /media/usb/monolith-amd64 /opt/edge_iot_offline && cd /opt/edge_iot_offline

# 一键:校验和 -> docker load -> arch 自检 -> compose up
./load-and-up.sh
```

`load-and-up.sh` 会:校验 `images.tar.sha256` → `docker load` → 打印每个镜像
`arch`(必须都是 amd64)→ `docker compose up -d`。

仅自检不起栈:`./load-and-up.sh --load-only`。
带硬件无关 smoke(起 mock-modbus/mock-mqtt):`./load-and-up.sh --profile smoke`。

---

## 4. 健康校验

```bash
curl -fsS -o /dev/null -w '%{http_code}\n' http://localhost/      # 期望 200
docker compose -f docker-compose.yml ps                           # 容器 Up/healthy
```

红线复述:对外只暴露 **:80**(nginx),django 仅容器内 :8000,不对外、无 `/health` 路由
(同 prod 实况,见 [[prod-deployment]])。

---

## 5. 升级既有装机(保留数据 —— 红线)

单体用 SQLite,镜像里烤了一份**全新** `db.sqlite3`,仅适合首装。
**升级时绝不能让新镜像覆盖现网 db。** 做法:

1. 备份现网 db:`cp /opt/edge_iot/app/backend/db.sqlite3 ~/db.bak.$(date +%s)`。
2. 在 `docker-compose.yml` 的 `django` 服务取消注释 `volumes:`,把宿主现网
   `db.sqlite3` 挂进 `/app/db.sqlite3`,这样 `migrate` 在现有数据上增量执行。
3. influx 数据在 named volume `influxdb-data`,`up` 不会清空。

---

## 6. 与分布式套件共存红线

- 单体走 **:80**;分布式 center 走 **:8002**、edge 历史口 **:18088/:18089**;
  mosquitto 单体 smoke :1883 vs 分布式 center :1883 ——**同机共存时务必错开**或不同机部署。
- **严禁在 prod/工控机本机 `docker compose --build`**(prod 上 `pip install` 把 1.7 GiB
  机器打爆过,见 [[prod-deployment]] 事故记录)。本套件只 `load` + `up`,无 pip/npm。

---

## 7. 关停与回滚

```bash
cd /opt/edge_iot_offline
docker compose -f docker-compose.yml down            # 停 + 删容器,保留 volume
docker compose -f docker-compose.yml down -v         # 连数据一起删(慎用)
```

开发/测试跑完按板规 [[shutdown-after-dev-test]] 关停所有本地进程/容器。
