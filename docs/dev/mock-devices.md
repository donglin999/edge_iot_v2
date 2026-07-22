# 模拟设备 —— 无硬件联调

现场没有设备也能把整条链路真跑起来：配置 → 采集 → 入库 → 前端曲线 →
掉线告警 → 自动重连。走的是和真设备**完全一样**的路径
（`BaseProtocol` → `ReadWorker` → sink），只有最底下那层 I/O 是编的，
所以看到的行为就是真实行为。

## 快速开始

```bash
cd backend

# 1) 后端 web（注意开关：不带的话前端「测试连接」会报 Protocol 'simulator' not registered）
EDGE_ENABLE_SIMULATOR=1 python manage.py runserver 127.0.0.1:8000

# 2) 前端
cd ../frontend && VITE_PROXY_TARGET=http://127.0.0.1:8000 npm run dev

# 3) 起模拟设备并开始采集（另开一个终端）
cd backend && EDGE_ENABLE_SIMULATOR=1 python manage.py run_mock_devices
```

浏览器打开 `http://127.0.0.1:5173/devices`，三台「模拟设备」应显示**在线**。

`Ctrl-C` 停止 —— 会话置为 stopped，设备随即回到离线。

## 常用参数

| 参数 | 说明 |
|------|------|
| `--devices 5` | 台数（默认 3），每台自动分配不同波形，图上能分辨 |
| `--points 4` | 每台测点数（温度/压力/转速/运行状态） |
| `--rate 2` | 采集频率 Hz |
| `--fail connect` | 故障注入：连不上 |
| `--fail read` | 连得上但读不到 |
| `--fail flaky` | 间歇性掉线 —— 演示告警起落与自动重连 |
| `--seed-only` | 只建配置，到前端「采集控制」手动启动 |
| `--reset` | 先清掉同前缀的旧模拟设备 |

## 演练：掉线 → 告警 → 自动重连 → 告警清除

```bash
EDGE_ENABLE_SIMULATOR=1 python manage.py run_mock_devices --fail flaky --rate 2
```

`flaky` 会真的把链路断掉（不只是读失败），worker 因此走重连分支。
在「告警中心」能看到连接告警反复起落，设备状态在在线/离线之间切换。
实测 35 秒内 13 次告警起落、18 次重连。

只想看「一直连不上」：`--fail connect` —— 设备恒为离线，告警一直 firing。

## 为什么默认关闭

`simulator` 是假协议，生产环境的协议下拉里不该出现。所以它只在
`EDGE_ENABLE_SIMULATOR=1` 时才进注册表 —— **开关按进程生效**，
web / celery / 采集三个进程都要带上才算完整可用。

## InfluxDB

写不进去时采集照常跑（状态、告警、连接测试都正常），只有「数据可视化」没有曲线。
命令启动时会先探一次并把该设的环境变量打印出来：

```bash
INFLUXDB_HOST=127.0.0.1 INFLUXDB_PORT=8086 \
INFLUXDB_TOKEN=<token> INFLUXDB_ORG=<org> INFLUXDB_BUCKET=<bucket> \
EDGE_ENABLE_SIMULATOR=1 python manage.py run_mock_devices
```
