#!/usr/bin/env python3
"""中山小家电 SCADA → InfluxDB 直采（应急单脚本 / 高吞吐版）

【状态】主系统的 scada 协议（backend/acquisition/protocols/scada.py）已按同样的
报文结构修正并全链路测试，正常情况用主系统采集即可。本脚本保留作应急兜底：
主系统不可用/升级窗口期，可单文件塞进任何有 paho+influxdb-client 的环境直跑。
注意它的存储格式（measurement=设备ID, field=中文名）与主系统 InfluxDBSink 不同。

    measurement = 设备ID(deviceCode)    field = 测点中文名    tag = code

报文真实结构（网关实际下发，payload 的 data 字段）：

    {"data": {
        "deviceCode":    "AN300400152600059",
        "propertyCode":  "N180100560001",
        "dataType":      2,
        "propertyValue": "2",
        "time":          "1755653755532"      # 字符串毫秒
    }}

只采 DEVICE_NAME 这一台设备的 POINTS 里那几个测点（精确订阅，不用通配符）。
设备ID 与测点编码直接取自报文（比解析话题更可靠）；话题解析仅作兜底。

不限频：来多少收多少。MQTT 回调只解析+入队（绝不阻塞网络线程），
独立写线程攒批落库，避免高频下堵塞。

依赖 paho-mqtt + influxdb-client —— 工控机无网装不了，直接用后端镜像跑：

    # /run/secrets/scada-collect.env 需 chmod 600，不得放入 Git/日志
    # 必填:SCADA_MQTT_BROKER/PORT/USERNAME/PASSWORD/CA_FILE、
    #      SCADA_PRODUCT_KEY/DEVICE_NAME、INFLUXDB_URL/TOKEN/ORG/BUCKET
    docker run -d --name scada-collect --restart unless-stopped --network host \
      --env-file /run/secrets/scada-collect.env \
      -v /run/secrets/plant-mqtt-ca.pem:/run/secrets/plant-mqtt-ca.pem:ro \
      -v /apps/edge_iot/scada_collect.py:/collect.py \
      edge-iot/backend:offline-amd64 python /collect.py

    docker logs -f scada-collect
"""
import json
import os
import queue
import re
import ssl
import threading
import time

import paho.mqtt.client as mqtt
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS


def required_env(name):
    """Read a required runtime value without ever logging its contents."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def required_port(name):
    raw = required_env(name)
    try:
        value = int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer") from exc
    if not 1 <= value <= 65535:
        raise SystemExit(f"{name} must be between 1 and 65535")
    return value


# ---------------- MQTT ----------------
BROKER = required_env("SCADA_MQTT_BROKER")
PORT = required_port("SCADA_MQTT_PORT")
USERNAME = required_env("SCADA_MQTT_USERNAME")
PASSWORD = required_env("SCADA_MQTT_PASSWORD")
MQTT_CA_FILE = required_env("SCADA_MQTT_CA_FILE")
USE_TLS = True
MQTT_VERSION = mqtt.MQTTv311      # 连不上可试 mqtt.MQTTv5
PRODUCT_KEY = required_env("SCADA_PRODUCT_KEY")
DEVICE_NAME = required_env("SCADA_DEVICE_NAME")   # 只采这一台设备

# ---------------- InfluxDB ----------------
INFLUX_URL = required_env("INFLUXDB_URL")
INFLUX_TOKEN = required_env("INFLUXDB_TOKEN")
INFLUX_ORG = required_env("INFLUXDB_ORG")
INFLUX_BUCKET = required_env("INFLUXDB_BUCKET")

# ---------------- 吞吐调优 ----------------
QUEUE_MAX = 200_000     # 有界缓冲；满了丢最旧的（保新数据），内存不会失控
BATCH_MAX = 5_000       # 每批最多写多少个点（堵了就调大）
FLUSH_SEC = 1.0         # 攒批最长等待
WRITERS = 2             # 写线程数（堵了就调到 4）
STATS_SEC = 10          # 统计打印间隔
DEBUG_FIRST = 3         # 先打印前几条原始报文供核对，之后只打统计

# ---------------- 过期点处理 ----------------
# 网关会推 retained 消息，带的是上次发布的老时间戳；比 bucket 保留期还旧的点
# InfluxDB 会直接拒收（422 "points beyond retention policy"）。这里先行拦掉。
# 用 check_retention 命令查你 bucket 的真实保留期，把 RETENTION_SEC 设得比它略小。
RETENTION_SEC = 29 * 24 * 3600   # 略小于 bucket 保留期（默认按 30 天算）
STALE_POLICY = "skip"            # skip=不存(推荐,不伪造时间) | ingest=改用到达时间 | keep=照发(会被Influx拒)

# 要采的测点：编码 → 中文名（用作 field 名）。只采这几个，别的一律不收。
POINTS = {
    "N270400150027": "注射压力实际值",
    "N270400151293": "注射速度实际值",
    "N270400151294": "注射位置实际值",
    "F40040100030008": "射胶时间实际值",
}

# 精确订阅这台设备的这几个测点话题（不用通配符，broker 只会推这 4 个话题给我们）
TOPICS = [
    (f"/sys/{PRODUCT_KEY}/device/{DEVICE_NAME}/thing/property/{code}/post", 0)
    for code in POINTS
]

influx = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG, timeout=30_000)
write_api = influx.write_api(write_options=SYNCHRONOUS)   # 我们自己攒批，一批一次 HTTP

q: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
stop = threading.Event()
lock = threading.Lock()
stats = {"recv": 0, "written": 0, "dropped": 0, "skipped": 0, "stale": 0,
         "retimed": 0, "rejected": 0, "errors": 0, "seen": 0}


def bump(key, n=1):
    with lock:
        stats[key] += n


def to_ns(raw):
    """时间戳归一到纳秒。网关给的是字符串毫秒("1755653755532")，也兼容秒/微秒/纳秒。"""
    try:
        t = int(raw)
    except (TypeError, ValueError):
        return time.time_ns()
    if t <= 0:
        return time.time_ns()
    if t < 10**11:
        return t * 10**9   # 秒
    if t < 10**14:
        return t * 10**6   # 毫秒  ← 网关实际用这个
    if t < 10**17:
        return t * 10**3   # 微秒
    return t               # 纳秒


def parse_reading(topic, payload):
    """解析成 (device_id, code, value, ts_ns)；解析不出返回 None。

    主路径 = 网关真实结构 payload["data"]；兜底 = 话题解析 + 常见结构猜测。
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, str):          # 个别网关会把 data 双重编码成 JSON 字符串
        try:
            data = json.loads(data)
        except Exception:
            data = None

    # ---- 主路径：{"data": {deviceCode, propertyCode, propertyValue, time}} ----
    if isinstance(data, dict) and "propertyValue" in data:
        device_id = str(data.get("deviceCode") or "").strip()
        code = str(data.get("propertyCode") or "").strip()
        if device_id and code:
            return device_id, code, data.get("propertyValue"), to_ns(data.get("time"))

    # ---- 兜底：话题 /sys/{pk}/device/{dn}/thing/property/{code}/post ----
    parts = topic.split("/")
    if len(parts) < 9:
        return None
    device_id, code = parts[4], parts[7]
    value = _guess_value(payload, code)
    if value is None:
        return None
    return device_id, code, value, _guess_ts_ns(payload)


def _guess_value(payload, code):
    """兜底取值：标量 / {"value":..} / {code:..} / {"params":{code:{"value":..}}}"""
    if isinstance(payload, (int, float, bool, str)):
        return payload
    if isinstance(payload, dict):
        if "value" in payload:
            return payload["value"]
        if code in payload:
            v = payload[code]
            return v["value"] if isinstance(v, dict) and "value" in v else v
        params = payload.get("params")
        if isinstance(params, dict) and code in params:
            v = params[code]
            return v["value"] if isinstance(v, dict) and "value" in v else v
    return None


def _guess_ts_ns(payload):
    if isinstance(payload, dict):
        for key in ("time", "timestamp", "ts"):
            if key in payload:
                return to_ns(payload[key])
    return time.time_ns()


def on_connect(client, userdata, flags, rc, *_):
    print(f"[MQTT] 已连接 rc={rc} → 订阅 {DEVICE_NAME} 的 {len(TOPICS)} 个测点话题:", flush=True)
    for topic, _qos in TOPICS:
        print(f"         {topic}", flush=True)
    client.subscribe(TOPICS)


def on_disconnect(client, userdata, rc, *_):
    print(f"[MQTT] 断开 rc={rc}，paho 会自动重连", flush=True)


def on_message(client, userdata, msg):
    """只做解析+入队，绝不阻塞 paho 网络线程"""
    bump("recv")
    raw = msg.payload
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        payload = raw.decode("utf-8", "ignore")

    with lock:
        seen = stats["seen"]
        if seen < DEBUG_FIRST:
            stats["seen"] += 1
    if seen < DEBUG_FIRST:
        print(f"[样例{seen + 1}] topic={msg.topic}\n         payload={raw[:300]!r}", flush=True)

    parsed = parse_reading(msg.topic, payload)
    if parsed is None:
        bump("skipped")
        return
    device_id, code, value, ts_ns = parsed

    # 只采配置里的这几个测点；别的一律不存（精确订阅下正常不会走到这）
    if code not in POINTS:
        bump("skipped")
        return

    # 过期点：retained 消息常带很老的时间戳，超出 bucket 保留期会被 Influx 拒收
    if time.time_ns() - ts_ns > RETENTION_SEC * 10**9:
        if STALE_POLICY == "skip":
            bump("stale")
            return
        if STALE_POLICY == "ingest":
            ts_ns = time.time_ns()
            bump("retimed")
        # keep → 原样发出（会被 Influx 丢弃，只用于排查）

    # propertyValue 是字符串（如 "2"）；能转数值就转，转不了就按字符串存
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = str(value)

    point = (
        Point(device_id)                          # measurement = 设备ID(deviceCode)
        .tag("code", code)
        .field(POINTS[code], value)               # field = 测点中文名
        .time(ts_ns, WritePrecision.NS)
    )
    try:
        q.put_nowait(point)
    except queue.Full:
        try:                                      # 丢最旧的，保住最新数据
            q.get_nowait()
            q.put_nowait(point)
            bump("dropped")
        except queue.Empty:
            pass


def writer():
    """批量落库：一批一次 HTTP，失败退避重试"""
    while not (stop.is_set() and q.empty()):
        try:
            batch = [q.get(timeout=0.5)]
        except queue.Empty:
            continue
        deadline = time.monotonic() + FLUSH_SEC
        while len(batch) < BATCH_MAX and time.monotonic() < deadline:
            try:
                batch.append(q.get_nowait())
            except queue.Empty:
                break
        for attempt in range(3):
            try:
                write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=batch)
                bump("written", len(batch))
                break
            except Exception as exc:
                text = str(exc)
                # 422 部分写入：合规的点已经入库，只有超出保留期的点被 Influx 丢弃。
                # 重试没有意义（数据一样还会被拒），且会把已写的点再写一遍。
                if "beyond retention policy" in text or "unprocessable" in text.lower():
                    n = 0
                    match = re.search(r"dropped=(\d+)", text)
                    if match:
                        n = int(match.group(1))
                    bump("rejected", n or 1)
                    bump("written", max(len(batch) - n, 0))
                    print(f"[部分写入] {len(batch)} 点中 {n} 点时间戳超出保留期被丢弃，"
                          f"其余已入库（调 RETENTION_SEC / STALE_POLICY 可消除）", flush=True)
                    break
                if attempt == 2:
                    bump("errors")
                    print(f"[写入失败] 丢弃 {len(batch)} 点: {exc}", flush=True)
                else:
                    time.sleep(0.5 * (2 ** attempt))


def stats_loop():
    last = dict(stats)
    while not stop.wait(STATS_SEC):
        with lock:
            cur = dict(stats)
        rate = (cur["written"] - last["written"]) / STATS_SEC
        print(
            f"[统计] 收={cur['recv']} 写={cur['written']} ({rate:.0f}/s) "
            f"丢={cur['dropped']} 跳过={cur['skipped']} 过期={cur['stale']} "
            f"改时={cur['retimed']} 拒收={cur['rejected']} 错={cur['errors']} 队列={q.qsize()}",
            flush=True,
        )
        last = cur


def main():
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=MQTT_VERSION)
    except (AttributeError, TypeError):
        client = mqtt.Client(protocol=MQTT_VERSION)   # 兼容老版 paho

    if USERNAME:
        client.username_pw_set(USERNAME, PASSWORD)
    if USE_TLS:
        # 现场自签/内部 CA 必须显式挂载；保留证书链和主机名校验。
        client.tls_set_context(ssl.create_default_context(cafile=MQTT_CA_FILE))
    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect

    workers = [threading.Thread(target=writer, daemon=True) for _ in range(WRITERS)]
    reporter = threading.Thread(target=stats_loop, daemon=True)
    for t in workers:
        t.start()
    reporter.start()

    print(
        f"[启动] 连接 {BROKER}:{PORT} TLS={USE_TLS} "
        f"队列上限={QUEUE_MAX} 批大小={BATCH_MAX} 写线程={WRITERS}",
        flush=True,
    )
    client.connect(BROKER, PORT, keepalive=60)
    try:
        client.loop_forever()          # 断线自动重连
    except KeyboardInterrupt:
        print("\n[退出] 收尾中，把队列剩余数据写完...", flush=True)
    finally:
        stop.set()
        client.disconnect()
        for t in workers:
            t.join(timeout=15)
        influx.close()
        print(
            f"[结束] 收={stats['recv']} 写={stats['written']} "
            f"丢={stats['dropped']} 跳过={stats['skipped']} 错={stats['errors']}",
            flush=True,
        )


if __name__ == "__main__":
    main()
