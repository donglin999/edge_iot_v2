# 离线部署 —— 中山小家电单体版（amd64）

只有 Docker、无网络的工控机上，一键部署整套边缘 IoT 数采系统（Django + Celery +
前端 + scada 协议）。

**架构约定（重要）**：所有服务用 **host 网络**。
- **InfluxDB 由你的生产环境自行运行**。地址、org、bucket 和 token
  必须在受保护的 `.env` 文件中显式配置；本包**不启动 InfluxDB**。
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
| `.env.example` | 不可直接运行的空值模板（不含密钥/现场值） |
| `load-and-up.sh` | **一键加载 + 启动** |
| `validate-env.sh` | 启动前校验密钥、安全开关和文件权限（不输出敏感值） |
| `manifest.txt` | 镜像清单 + 架构 + 大小 |
| `README.md` | 本文档 |

> **前置**：
> 1. 目标机须已有 `redis:7.0.10` 镜像（`docker images` 可见）；若宿主机已自跑 Redis，删 compose 里 redis 服务。
> 2. 宿主机须已运行 InfluxDB，并准备有限权限的 API token。
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

# 首次必须生成受保护的生产配置。模板中的密钥/现场值故意留空。
umask 077
cp .env.example .env
chmod 600 .env
openssl rand -hex 32       # 将输出填入 SECRET_KEY，不要复制到群聊或工单
vi .env                    # 填 ALLOWED_HOSTS 和 InfluxDB org/bucket/token

# 首次：完整模式（要用界面配置）
./load-and-up.sh --env-file .env

# 配置完成后，切到省资源的采集模式
docker compose --env-file .env -f docker-compose.yml stop django web
# 或下次直接：
./load-and-up.sh --env-file .env --headless

# 需要再开界面
docker compose --env-file .env --profile ui -f docker-compose.yml up -d
```

`load-and-up.sh` 会先执行 fail-closed 校验：`.env` 必须是权限 `0400` 或
`0600` 的普通文件，`DEBUG=False`，Django key 与 Influx token 不能为空/占位值，
`ALLOWED_HOSTS` 不能是 `*`。任何一项失败都会在 `docker load`、删容器或停旧服务前退出。

> **资源调优**：`CELERY_ACQ_CONCURRENCY` 安全默认为 32。大规模现场必须压测后
> 再显式调高；几台设备可调到 `8`~`16` 继续节省内存。

`ALLOW_PRIVATE_NETWORK_TESTS=False` 不影响已配置的采集任务，只禁止页面“连接测试”
功能主动访问私网地址。仅完全受信的隔离内网/本地测试需要该能力时，
按 `.env.example` 同时设置开关与 SSRF 风险确认字段。

**验证**：

```bash
curl -fsS http://localhost/ >/dev/null && echo OK
docker compose --env-file .env -f docker-compose.yml ps   # 各服务 Up / healthy
```

浏览器访问 `http://<工控机IP>/` 即为前端。

---

## 3. 部署后：配置中山小家电数采任务（前端操作）

系统已内置 **scada** 协议（基于 MQTT）。在前端「设备/协议配置」里新建设备，选 `scada`，
按 `docs/scada-zhongshan-example.md` 填写：

- Broker：填现场域名/IP 与端口，启用 TLS，用户名/密码从受控凭据发放渠道获取
- product_key：`123daffb91264286adcdf3bfe55194c7`，device_name：`A0201010001150403`
- 话题模板：`/sys/{product_key}/device/{device_name}/thing/property/{code}/post`
- 4 个测点：`N270400150027`(注射压力)、`N270400151293`(注射速度)、
  `N270400151294`(注射位置)、`F40040100030008`(射胶时间)

也可用前端「下载 Excel 配置模板」（自动含 scada 列）批量导入。启动采集后，
数据订阅自 MQTT 并写入 InfluxDB；故障会自动重启并在告警面板留痕。

---

## 4. 升级 / 保留数据

- **InfluxDB 数据**：在你自管的宿主机 InfluxDB 里，与本包生命周期无关，升级不受影响。
- **保留 SQLite 数据**：配置库 `/data/db.sqlite3` 与 Influx 失败落盘队列
  `/data/influx_spill.sqlite3` 都位于 `app-db` 持久卷，容器重建不会丢失。
  **红线：升级时绝不删除或覆盖该卷。**
- **失败队列语义**：一个 spill 文件只能对应同一套 Influx org/bucket/token；恢复写入采用
  at-least-once，进程在 Influx 已确认但尚未删除队列项时崩溃，重启后可能重放最后一批。
  正常采集点均带原始时间戳，可依靠 Influx 的 series + timestamp 覆盖语义收敛；不要把
  不带时间戳的自定义写入与该队列混用。异步写入在失败回调执行前发生硬崩溃，仍可能留下
  一个未落盘窗口，因此现场必须同时监控采集心跳与 `dropped_messages`。
- 升级：使用 `./load-and-up.sh --env-file .env`（或加 `--headless`）；禁止把 `.env` 打进安装包或提交到 Git。

## 5. 常见问题

- **`[缺] redis:7.0.10`**：目标机没有该镜像。若宿主机已自跑 Redis → 删 compose 里 redis 服务；
  否则先单独 `docker load` redis 镜像。
- **数据写不进 InfluxDB**：确认 `.env` 中的 host/port/org/bucket/token 与现场 InfluxDB
  一致（host 网络下容器内 127.0.0.1 = 宿主机）。不要在日志或工单中粘贴 token。
- **:80 被占用**：改宿主机上占用者，或改用 host 网络时需释放 80；host 网络下不支持端口重映射。
- **架构不符（exec format error）**：包是 amd64；确认目标机是 x86_64。arm64 需另出 arm64 包。
- **配置校验拒绝启动**：按错误信息补齐 `.env`，并执行 `chmod 600 .env`。不得绕过校验或恢复仓库内默认密钥。
- **host 网络**：Linux 工控机支持；容器直接用宿主机网络栈，无 `ports:` 映射。
