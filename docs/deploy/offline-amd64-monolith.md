# 离线部署 —— 中山小家电单体版（amd64）

只有 Docker、无网络的工控机上，一键部署整套边缘 IoT 数采系统（Django + Celery +
前端 + scada 协议）。

**架构约定（重要）**：所有服务用 **host 网络**。
- **InfluxDB 由你的生产环境自行运行**在宿主机 `127.0.0.1:8086`（org=`Midea`，bucket=`Record`）。
  本包**不启动 InfluxDB**，只连它。
- **Redis 由本包启动**（`redis:7.0.10`，目标机已有该镜像）。若宿主机已自跑 Redis 占用
  6379，删掉 compose 里的 redis 服务即可。
- Django 仅绑 `127.0.0.1:8000`（不对外）；nginx 前端绑 `:80`，是唯一对外入口。

---

## 0. 包内容

解包后 `zhongshan-monolith-amd64/`：

| 文件 | 说明 |
|------|------|
| `images.tar` | 应用镜像 `edge-iot/backend`、`edge-iot/web`（amd64，docker save） |
| `images.tar.sha256` | 校验和 |
| `docker-compose.yml` | 编排（host 网络；连宿主机 InfluxDB；本包起 redis+应用+前端） |
| `.env.example` | 可选覆盖项（密钥/Redis/Influx 连接） |
| `load-and-up.sh` | **一键加载 + 启动** |
| `manifest.txt` | 镜像清单 + 架构 + 大小 |
| `README.md` | 本文档 |

> **前置**：
> 1. 目标机须已有 `redis:7.0.10` 镜像（`docker images` 可见）；若宿主机已自跑 Redis，删 compose 里 redis 服务。
> 2. 宿主机须已运行 InfluxDB 且 `127.0.0.1:8086` 可达（org=Midea / bucket=Record / 配套 token）。
> `load-and-up.sh` 会先做这两项检查。

---

## 1. 构建离线包（在联网的构建机上，一次）

```bash
# 1) 先在宿主机构建前端静态包（不要在跨架构容器里构建 SPA）
cd frontend && npm ci && npm run build && cd ..

# 2) 一键构建离线包（自动：wheelhouse → buildx amd64 镜像 → docker save → 打包）
bash scripts/offline/build-offline-bundle.sh

# 产物：dist/offline/zhongshan-monolith-amd64/
```

把整个 `dist/offline/zhongshan-monolith-amd64/` 目录拷到 U 盘。

---

## 2. 部署（在无网工控机上，一键）

### 两种运行模式（省资源）

配置时才需要前端，平时只要采集在跑：

| 模式 | 起哪些 | 用途 |
|------|--------|------|
| **完整模式**（默认） | redis + migrate + celery + **django + web** | 配置网关/设备/测点、看可视化 |
| **采集模式** `--headless` | redis + migrate + celery | 日常跑数采，**省 ~100-150MB** |

采集模式下自愈/告警照常（看门狗在 celery 里），启动时会自动恢复上次 RUNNING 的会话。
`migrate` 是一次性容器（跑完即退），两种模式都执行，所以无界面也能自洽。

```bash
# U 盘目录拷到本地，进入目录
cd zhongshan-monolith-amd64

# 首次：完整模式（要用界面配置）
./load-and-up.sh
# 需要改配置时： ./load-and-up.sh --env-file .env   （先 cp .env.example .env 修改）

# 配置完成后，切到省资源的采集模式
docker compose stop django web          # 老版 compose 用 docker-compose
# 或下次直接：
./load-and-up.sh --headless

# 需要再开界面
docker compose --profile ui -f docker-compose.yml up -d
```

> **更大的一块资源在 celery 并发**：`CELERY_ACQ_CONCURRENCY` 默认 200 线程（为大规模现场准备）。
> 只有几台设备的工控机，在 `.env` 里调到 `8`~`32` 能省下可观内存，比关前端更有效。

**验证**：

```bash
curl -fsS http://localhost/ >/dev/null && echo OK
docker compose ps        # 各服务 Up / healthy
```

浏览器访问 `http://<工控机IP>/` 即为前端。

---

## 3. 部署后：配置中山小家电数采任务（前端操作）

系统已内置 **scada** 协议（基于 MQTT）。在前端「设备/协议配置」里新建设备，选 `scada`，
按 `docs/scada-zhongshan-example.md` 填写：

- Broker：`10.134.14.147:8883`，启用 TLS，用户名 `ZYY_XJDZS`，密码现场填写
- product_key：`123daffb91264286adcdf3bfe55194c7`，device_name：`A0201010001150403`
- 话题模板：`/sys/{product_key}/device/{device_name}/thing/property/{code}/post`
- 4 个测点：`N270400150027`(注射压力)、`N270400151293`(注射速度)、
  `N270400151294`(注射位置)、`F40040100030008`(射胶时间)

也可用前端「下载 Excel 配置模板」（自动含 scada 列）批量导入。启动采集后，
数据订阅自 MQTT 并写入 InfluxDB；故障会自动重启并在告警面板留痕。

---

## 4. 升级 / 保留数据

- **InfluxDB 数据**：在你自管的宿主机 InfluxDB 里，与本包生命周期无关，升级不受影响。
- **保留 SQLite 配置库**：默认库烤在镜像内（全新装）。要保留既有配置，按
  `docker-compose.yml` 里 django 服务的注释挂载磁盘上的 `db.sqlite3`。**红线：绝不覆盖已存在的库。**
- 升级：`docker load` 新 `images.tar` 后 `docker compose up -d` 即可。

## 5. 常见问题

- **`[缺] redis:7.0.10`**：目标机没有该镜像。若宿主机已自跑 Redis → 删 compose 里 redis 服务；
  否则先单独 `docker load` redis 镜像。
- **数据写不进 InfluxDB**：确认宿主机 InfluxDB 已启动、`127.0.0.1:8086` 可达，且
  org=`Midea`/bucket=`Record`/token 与 `.env` 一致（host 网络下容器内 127.0.0.1 = 宿主机）。
- **:80 被占用**：改宿主机上占用者，或改用 host 网络时需释放 80；host 网络下不支持端口重映射。
- **架构不符（exec format error）**：包是 amd64；确认目标机是 x86_64。arm64 需另出 arm64 包。
- **生产加固**：内网隔离机默认 `DEBUG=True` 可用；对外可达时在 `.env` 设 `DEBUG=False` 并换 `SECRET_KEY`。
- **host 网络**：Linux 工控机支持；容器直接用宿主机网络栈，无 `ports:` 映射。
