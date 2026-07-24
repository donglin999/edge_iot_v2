#!/usr/bin/env python3
"""一条命令托管「除 modbus 外」的全部协议 mock 对端,供本地联调。

    python3 acquisition/testing/host_protocol_mocks.py

托管内容(全部真实对端,协议侧走真实 I/O):
  - OPC-UA 真服务器  opc.tcp://127.0.0.1:15340/freeopcua/mock/(9 节点,值随时间变化)
  - S7 真服务器      127.0.0.1:15402(snap7,DB1 布局,本脚本每秒刷新值)
  - MQTT/SCADA 发布器 → mosquitto(127.0.0.1:18883,匿名):
      · sensor/line1/data     JSON {temperature, humidity, running}   (mqtt 协议用)
      · /sys/{pk}/device/{dn}/thing/property/{code}/post              (scada 协议用,
        真实网关负载结构 data.propertyValue)

modbus 的 mock 由 `manage.py run_modbus_mock` 托管(15020),不在本脚本内。
mosquitto 需已运行(docker edge-test-mosquitto @18883),脚本启动时会探测并提示。

Ctrl-C / SIGTERM 优雅退出。
"""
from __future__ import annotations

import json
import math
import signal
import socket
import sys
import threading
import time

sys.path.insert(0, ".")  # 从 backend/ 目录运行

from acquisition.testing.opcua_mock_server import OPCUAMockServer  # noqa: E402
from acquisition.testing.s7_mock_server import S7MockServer  # noqa: E402

import paho.mqtt.client as mqtt  # noqa: E402

MQTT_HOST, MQTT_PORT = "127.0.0.1", 18883
SCADA_PK = "mockpk0001"
SCADA_DEVICE = "MOCKDEV001"
SCADA_POINTS = {"P001": "注射压力", "P002": "注射速度"}


def _say(msg: str) -> None:
    print(msg, flush=True)


def check_mosquitto() -> bool:
    try:
        with socket.create_connection((MQTT_HOST, MQTT_PORT), timeout=2):
            return True
    except OSError:
        return False


def s7_update_loop(server: S7MockServer, stop: threading.Event) -> None:
    """S7 服务器只有静态写接口,这里每秒刷一轮,让曲线动起来。"""
    tick = 0
    while not stop.wait(1.0):
        tick += 1
        t = time.time()
        server.set_bit("DB", 0, 0, tick % 2 == 0)          # DB1.DBX0.0 bool 翻转
        server.write_int16("DB", 2, int(500 * math.sin(t / 9)))       # DB1.DBW2
        server.write_float32("DB", 4, 50 + 25 * math.sin(t / 7))      # DB1.DBD4
        server.write_float64("DB", 8, 1000 + 500 * math.sin(t / 11))  # DB1.DBD8 lreal


def mqtt_publish_loop(stop: threading.Event) -> None:
    client = mqtt.Client(client_id="mock-publisher")
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.loop_start()
    tick = 0
    try:
        while not stop.wait(1.0):
            tick += 1
            t = time.time()
            # mqtt 协议:一条 JSON 多字段
            client.publish("sensor/line1/data", json.dumps({
                "temperature": round(25 + 5 * math.sin(t / 10), 2),
                "humidity": round(50 + 10 * math.sin(t / 15), 1),
                "running": tick % 20 < 15,
            }))
            # scada 协议:每测点一话题,真实网关负载结构(值在 data.propertyValue)
            for code in SCADA_POINTS:
                value = round(80 + 15 * math.sin(t / 8 + hash(code) % 7), 2)
                topic = f"/sys/{SCADA_PK}/device/{SCADA_DEVICE}/thing/property/{code}/post"
                client.publish(topic, json.dumps({
                    "data": {
                        "deviceCode": SCADA_DEVICE,
                        "propertyCode": code,
                        "dataType": 2,
                        "propertyValue": str(value),
                        "time": str(int(t * 1000)),
                    }
                }))
    finally:
        client.loop_stop()
        client.disconnect()


def main() -> None:
    stop = threading.Event()

    def _sig(signum, frame):  # noqa: ARG001
        stop.set()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    if not check_mosquitto():
        _say(f"⚠ mosquitto 不可达({MQTT_HOST}:{MQTT_PORT})—— mqtt/scada 发布器不会启动。")
        _say("  启动方法见 acquisition/testing/mqtt_mock_broker.py(docker edge-test-mosquitto)")
        mqtt_ok = False
    else:
        mqtt_ok = True

    opcua = OPCUAMockServer(host="127.0.0.1", port=15340)
    opcua.start()
    _say(f"OPC-UA mock 已启动  {opcua.endpoint_url}(9 节点,值自更新)")

    s7 = S7MockServer(host="0.0.0.0", port=15402)
    s7_port = s7.start()
    _say(f"S7 mock 已启动      127.0.0.1:{s7_port}(DB1 布局,每秒刷新)")
    threading.Thread(target=s7_update_loop, args=(s7, stop), daemon=True).start()

    if mqtt_ok:
        threading.Thread(target=mqtt_publish_loop, args=(stop,), daemon=True).start()
        _say(f"MQTT/SCADA 发布器   → {MQTT_HOST}:{MQTT_PORT}"
             f"(sensor/line1/data + /sys/{SCADA_PK}/device/{SCADA_DEVICE}/...)")

    _say("")
    _say("全部 mock 就绪。Ctrl-C 停止。")
    try:
        while not stop.wait(0.5):
            pass
    finally:
        _say("正在停止…")
        opcua.stop()
        s7.stop()
        _say("已停止。")


if __name__ == "__main__":
    main()
