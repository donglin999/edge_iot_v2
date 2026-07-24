"""MQTTProtocol 用真实 mosquitto broker 联调。

mock 的是**对端**(broker),协议侧全走真实 paho-mqtt CONNECT/SUBSCRIBE/收
PUBLISH,解析走真正的 ``MQTTProtocol._parse_message``。broker 由
``acquisition.testing.mqtt_mock_broker.MosquittoMockBroker`` 起一个真实的
docker mosquitto 容器(容器名 edge-test-mosquitto,宿主端口 18883)。

docker 不可用时整个文件跳过(不降级为假 broker —— MQTT 传输层的假替身在
``tests/mocks/transports.py``,那条路径已经被 ``test_e2e_all_protocols.py``
覆盖过,这里的价值就在于走真 broker)。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

import pytest

from acquisition.protocols.mqtt import MQTTProtocol
from acquisition.testing.mqtt_mock_broker import MosquittoMockBroker, docker_available

pytestmark = pytest.mark.skipif(
    not docker_available(), reason="docker 不可用,跳过真 mosquitto broker 联调"
)

import paho.mqtt.client as mqtt  # noqa: E402  (after skip check keeps collection cheap)


# ===========================================================================
# fixtures
# ===========================================================================


@pytest.fixture(scope="module")
def broker():
    b = MosquittoMockBroker()
    try:
        b.start()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"mosquitto 容器起不来,跳过: {exc}")
    try:
        yield b
    finally:
        b.stop()


@pytest.fixture
def publisher(broker):
    """短命的发布端 paho 客户端,每个用例独立一个,用后即断。"""
    pub = mqtt.Client()
    pub.connect(broker.host, broker.host_port, keepalive=10)
    pub.loop_start()
    # 给连接一点时间握手,避免第一条 publish 在 CONNACK 之前发出。
    deadline = time.time() + 5
    while not pub.is_connected() and time.time() < deadline:
        time.sleep(0.05)
    yield pub
    pub.loop_stop()
    pub.disconnect()


def _publish(pub, topic: str, payload: Any, qos: int = 0, retain: bool = False) -> None:
    body = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
    info = pub.publish(topic, body, qos=qos, retain=retain)
    info.wait_for_publish(timeout=5)


def _connect_protocol(broker, topics: str, **extra) -> MQTTProtocol:
    cfg = {
        "source_ip": broker.host,
        "source_port": broker.host_port,
        "mqtt_topics": topics,
        "mqtt_read_timeout": 2.0,
        **extra,
    }
    proto = MQTTProtocol(cfg)
    assert proto.connect() is True
    return proto


def _wait_and_read(proto: MQTTProtocol, points: List[Dict[str, Any]], settle: float = 0.6):
    """给 broker 一点时间把消息推过来,再走真正的 read_points() 排空队列。"""
    time.sleep(settle)
    return proto.read_points(points)


def _by_code(results: List[Dict[str, Any]], code: str) -> Optional[Dict[str, Any]]:
    for r in results:
        if r["code"] == code:
            return r
    return None


# ===========================================================================
# 1. JSON payload 取值方式
# ===========================================================================


class TestPayloadShapes:
    def test_json_code_toplevel(self, broker, publisher):
        proto = _connect_protocol(broker, "shapes/toplevel")
        try:
            _publish(publisher, "shapes/toplevel", {"temperature": 26.5})
            res = _wait_and_read(
                proto, [{"code": "temperature", "data_type": "float"}]
            )
            r = _by_code(res, "temperature")
            assert r is not None and r["value"] == 26.5
        finally:
            proto.disconnect()

    def test_payload_path_nested(self, broker, publisher):
        proto = _connect_protocol(broker, "shapes/nested")
        try:
            _publish(publisher, "shapes/nested", {"data": {"value": 12.3}})
            res = _wait_and_read(
                proto,
                [{"code": "pressure", "payload_path": "data.value", "data_type": "float"}],
            )
            r = _by_code(res, "pressure")
            assert r is not None and r["value"] == 12.3
        finally:
            proto.disconnect()

    def test_bare_scalar(self, broker, publisher):
        proto = _connect_protocol(broker, "shapes/scalar")
        try:
            _publish(publisher, "shapes/scalar", 42.5)  # bare JSON number
            res = _wait_and_read(proto, [{"code": "level", "data_type": "float"}])
            r = _by_code(res, "level")
            assert r is not None and r["value"] == 42.5
        finally:
            proto.disconnect()

    def test_value_field_fallback(self, broker, publisher):
        proto = _connect_protocol(broker, "shapes/valuefield")
        try:
            _publish(publisher, "shapes/valuefield", {"value": 7})
            res = _wait_and_read(proto, [{"code": "count", "data_type": "int"}])
            r = _by_code(res, "count")
            assert r is not None and r["value"] == 7
        finally:
            proto.disconnect()


# ===========================================================================
# 2. 数据类型
# ===========================================================================


class TestDataTypes:
    @pytest.mark.parametrize(
        "raw,data_type,expected",
        [
            (26.5, "float", 26.5),
            ("26.5", "float", 26.5),
            (7, "int", 7),
            ("7", "int", 7),
            (True, "bool", True),
            ("true", "bool", True),
            ("0", "bool", False),
            ("hello", "string", "hello"),
            (123, "string", "123"),
        ],
    )
    def test_data_type_coercion(self, broker, publisher, raw, data_type, expected):
        topic = f"types/{data_type}/{raw!r}".replace(" ", "_")
        proto = _connect_protocol(broker, topic)
        try:
            _publish(publisher, topic, {"v": raw})
            res = _wait_and_read(proto, [{"code": "v", "data_type": data_type}])
            r = _by_code(res, "v")
            assert r is not None, f"no reading for raw={raw!r} data_type={data_type}"
            assert r["value"] == expected

            # data_type is declared but must not affect topic wiring.
        finally:
            proto.disconnect()


# ===========================================================================
# 3. 话题通配符 (+ / #) + topic_filter + 同 topic 多测点
# ===========================================================================


class TestTopicsAndMultiPoint:
    def test_plus_wildcard(self, broker, publisher):
        proto = _connect_protocol(broker, "wild/+/temp")
        try:
            _publish(publisher, "wild/line1/temp", {"t": 1.0})
            _publish(publisher, "wild/line2/temp", {"t": 2.0})
            res = _wait_and_read(proto, [{"code": "t", "data_type": "float"}])
            values = sorted(r["value"] for r in res if r["code"] == "t")
            assert values == [1.0, 2.0]
        finally:
            proto.disconnect()

    def test_hash_wildcard(self, broker, publisher):
        proto = _connect_protocol(broker, "wild2/#")
        try:
            _publish(publisher, "wild2/a/b/c", {"t": 3.0})
            res = _wait_and_read(proto, [{"code": "t", "data_type": "float"}])
            r = _by_code(res, "t")
            assert r is not None and r["value"] == 3.0
        finally:
            proto.disconnect()

    def test_same_topic_multiple_points_via_topic_filter(self, broker, publisher):
        """同一个订阅 (#) 下,两个测点各用 topic_filter 精确认领自己的消息。"""
        proto = _connect_protocol(broker, "multi/#")
        try:
            _publish(publisher, "multi/deviceA/status", {"status": 1})
            _publish(publisher, "multi/deviceB/status", {"status": 2})
            points = [
                {"code": "status", "topic_filter": "multi/deviceA/+", "data_type": "int"},
                {"code": "status", "topic_filter": "multi/deviceB/+", "data_type": "int"},
            ]
            res = _wait_and_read(proto, points)
            by_topic = {r["topic"]: r["value"] for r in res}
            assert by_topic.get("multi/deviceA/status") == 1
            assert by_topic.get("multi/deviceB/status") == 2
        finally:
            proto.disconnect()

    def test_same_topic_multiple_fields(self, broker, publisher):
        """同一条消息里多个字段各自映射到不同测点(不需要 topic_filter)。"""
        proto = _connect_protocol(broker, "multi2/combo")
        try:
            _publish(publisher, "multi2/combo", {"temperature": 25.0, "humidity": 55})
            points = [
                {"code": "temperature", "data_type": "float"},
                {"code": "humidity", "data_type": "int"},
            ]
            res = _wait_and_read(proto, points)
            assert _by_code(res, "temperature")["value"] == 25.0
            assert _by_code(res, "humidity")["value"] == 55
        finally:
            proto.disconnect()


# ===========================================================================
# 4. retained 消息 + QoS
# ===========================================================================


class TestRetainedAndQos:
    def test_retained_message_delivered_on_subscribe(self, broker, publisher):
        topic = "retained/last"
        # 发布方先发一条 retained 消息,此时还没有订阅者。
        _publish(publisher, topic, {"v": 99}, retain=True)
        time.sleep(0.3)

        proto = _connect_protocol(broker, topic)
        try:
            # 订阅之后,broker 应主动把 retained 消息重放给新订阅者 —— 不需要
            # publisher 再发一次。
            res = _wait_and_read(proto, [{"code": "v", "data_type": "int"}])
            r = _by_code(res, "v")
            assert r is not None and r["value"] == 99
        finally:
            proto.disconnect()
            # 清掉 retained 消息,避免污染后续用例(空 payload = 删除)。
            _publish(publisher, topic, b"", retain=True)

    @pytest.mark.parametrize("qos", [0, 1])
    def test_qos_levels(self, broker, publisher, qos):
        topic = f"qos/{qos}"
        proto = _connect_protocol(broker, topic, mqtt_qos=qos)
        try:
            _publish(publisher, topic, {"v": 1}, qos=qos)
            res = _wait_and_read(proto, [{"code": "v", "data_type": "int"}])
            r = _by_code(res, "v")
            assert r is not None and r["value"] == 1
        finally:
            proto.disconnect()


# ===========================================================================
# 5. 断连收敛
# ===========================================================================


class TestReconnection:
    def test_health_check_reflects_broker_down_then_up(self, broker, publisher):
        proto = _connect_protocol(broker, "reconnect/topic")
        try:
            assert proto.health_check() is True

            broker.docker_stop()
            # 给 paho 的网络线程一点时间发现 socket 断了并回调 on_disconnect。
            deadline = time.time() + 10
            while proto.health_check() and time.time() < deadline:
                time.sleep(0.2)
            assert proto.health_check() is False
            assert proto.is_connected is False

            broker.docker_start()
            # paho 默认 reconnect_on_failure=True,后台线程会自动重连;
            # 用一个新连接/或等待自动重连均可,这里直接验证「重新 connect()
            # 之后能继续收消息」这个更贴近 ReadWorker 实际路径的行为。
            assert proto.connect() is True
            assert proto.health_check() is True

            pub2 = mqtt.Client()
            pub2.connect(broker.host, broker.host_port, keepalive=10)
            pub2.loop_start()
            time.sleep(0.3)
            _publish(pub2, "reconnect/topic", {"v": 5})
            pub2.loop_stop()
            pub2.disconnect()

            res = _wait_and_read(proto, [{"code": "v", "data_type": "int"}])
            r = _by_code(res, "v")
            assert r is not None and r["value"] == 5
        finally:
            proto.disconnect()


# ===========================================================================
# 6. 队列语义(maxsize=1000,满了怎么办)
# ===========================================================================


class TestQueueSemantics:
    def test_queue_full_drops_without_blocking(self, broker, publisher):
        """灌爆 data_queue(maxsize=1000):真实行为是丢弃且不阻塞 on_message
        回调,并且 mqtt.py 已经在 _on_message 里对 queue.Full 打了 warning
        日志(不是静默丢)。这里如实验证这两点,不再重复加日志。

        用手工挂一个 logging.Handler 到协议的具体 logger 上收集记录 ——
        不用 pytest 的 caplog:项目 LOGGING 配置里 "acquisition" 这个 logger
        是 propagate=False(见 control_plane/settings.py),caplog 默认挂在
        root logger 上收不到,这里直接测「日志真的响了」而不是巧合能不能被
        pytest 插件看见。
        """
        import logging

        records: list[logging.LogRecord] = []

        class _Collector(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        target_logger = logging.getLogger("acquisition.protocols.mqtt.MQTTProtocol")
        handler = _Collector(level=logging.WARNING)
        target_logger.addHandler(handler)

        proto = _connect_protocol(broker, "queue/flood")
        try:
            n = 1200  # > maxsize=1000
            for i in range(n):
                _publish(publisher, "queue/flood", {"v": i})
            # 消息是异步进 on_message 回调的,给点时间让它们都处理完。
            deadline = time.time() + 15
            while proto.data_queue.qsize() < 1000 and time.time() < deadline:
                time.sleep(0.1)

            assert proto.data_queue.qsize() <= 1000, "队列不该超过 maxsize"
            # 灌了 1200 条超过 maxsize=1000,多出来的必须被丢弃(而不是阻塞
            # on_message 回调把网络线程卡死)——验证 put 是非阻塞的:上面的
            # while 循环必须在 deadline 前收敛到 <=1000,而不是一直增长。
            assert any(
                "full" in rec.getMessage().lower() or "dropping" in rec.getMessage().lower()
                for rec in records
            ), "queue.Full 时应有 warning 日志(mqtt.py _on_message 已有,这里验证它确实触发了)"
        finally:
            target_logger.removeHandler(handler)
            proto.disconnect()


def test_read_points_returns_under_continuous_stream(broker):
    """持续消息流下 read_points 必须在一个快照后返回,不能被流喂到永不归。

    以前的排空循环是「每条 get(timeout) 直到空」:消息到达间隔 < read_timeout 时
    (1Hz 流 vs 2s 超时),永远等得到下一条 —— read_points 卡死。有限条消息的
    测试暴露不了,本用例用后台线程持续发布来复现。
    """
    import json as _json
    import threading as _threading
    import time as _time

    import paho.mqtt.client as _mqtt

    from acquisition.protocols.mqtt import MQTTProtocol

    stop = _threading.Event()

    def _publisher():
        pub = _mqtt.Client(client_id="cont-pub")
        pub.connect(broker.host, broker.host_port, keepalive=10)
        pub.loop_start()
        while not stop.wait(0.3):  # 0.3s 间隔 << 2s read_timeout
            pub.publish("cont/stream", _json.dumps({"v": _time.time()}))
        pub.loop_stop(); pub.disconnect()

    t = _threading.Thread(target=_publisher, daemon=True)
    t.start()
    try:
        proto = MQTTProtocol({
            "source_ip": broker.host, "source_port": broker.host_port,
            "mqtt_topics": "cont/stream", "mqtt_use_tls": False,
            "mqtt_qos": 0, "mqtt_read_timeout": 2.0,
        })
        assert proto.connect()
        _time.sleep(1.0)  # 攒几条

        started = _time.time()
        results = proto.read_points([{"code": "v", "data_type": "float"}])
        elapsed = _time.time() - started

        # 必须远小于「被流卡死」的量级;给宽裕上限 6s(首条等待 2s + 排空)
        assert elapsed < 6.0, f"read_points 被持续流卡了 {elapsed:.1f}s"
        assert results, "攒了 1s 的消息,至少要读到一条"
        proto.disconnect()
    finally:
        stop.set(); t.join(timeout=3)
