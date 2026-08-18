"""SCADAProtocol 用真实 mosquitto broker 联调。

mock 的是**对端**(broker/网关),协议侧全走真实 paho-mqtt CONNECT/SUBSCRIBE/
收 PUBLISH,解析走真正的 ``SCADAProtocol._parse_message``/``_extract_value``。
broker 由 ``acquisition.testing.mqtt_mock_broker.MosquittoMockBroker`` 起一个
真实的 docker mosquitto 容器(容器名 edge-test-mosquitto-pytest,复用 MQTT 用例同一个
helper,宿主端口 18893)。

真实网关负载结构参考 ``tests/test_e2e_all_protocols.py`` 的 ``SCRIPTS['scada']``
(``data.propertyValue`` / ``data.time`` 毫秒时间戳 —— 6e15018 修的就是这个)。

不测 TLS 握手本身(mosquitto:2 默认镜像没有现成证书,搭一套自签证书链的
投入产出比对「验证协议解析逻辑」这个目标来说不划算)——这里统一用
``mqtt_use_tls=False`` 连 broker,TLS 部分交给连接层本身(已经是
``ssl.SSLContext`` 走 paho 标准路径,不是 SCADAProtocol 自己写的),标注为
未覆盖场景。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

import pytest

from acquisition.protocols.scada import SCADAProtocol
from acquisition.testing.mqtt_mock_broker import MosquittoMockBroker, docker_available

pytestmark = pytest.mark.skipif(
    not docker_available(), reason="docker 不可用,跳过真 mosquitto broker 联调"
)

import paho.mqtt.client as mqtt  # noqa: E402

PRODUCT_KEY = "123daffb91264286adcdf3bfe55194c7"
DEVICE_NAME = "A0201010001150403"


def _wait_for_publisher_connection(pub, *, timeout: float = 10.0) -> None:
    """Wait until paho has processed CONNACK before publishing test traffic."""
    deadline = time.monotonic() + timeout
    while not pub.is_connected() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert pub.is_connected(), "publisher did not reconnect before the deadline"


def _topic(code: str, product_key: str = PRODUCT_KEY, device_name: str = DEVICE_NAME) -> str:
    return f"/sys/{product_key}/device/{device_name}/thing/property/{code}/post"


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
    pub = mqtt.Client()
    pub.connect(broker.host, broker.host_port, keepalive=10)
    pub.loop_start()
    try:
        _wait_for_publisher_connection(pub, timeout=5.0)
        yield pub
    finally:
        pub.loop_stop()
        pub.disconnect()


def _publish_gateway(
    pub,
    code: str,
    property_value: Any,
    *,
    data_type_enum: int = 2,
    time_ms: Optional[str] = None,
    product_key: str = PRODUCT_KEY,
    device_name: str = DEVICE_NAME,
) -> None:
    """发一条真实网关结构的报文:{"data": {..., "propertyValue": ..., "time": "<ms>"}}"""
    body = {
        "data": {
            "deviceCode": device_name,
            "propertyCode": code,
            "dataType": data_type_enum,
            "propertyValue": property_value,
            "time": time_ms or str(int(time.time() * 1000)),
        }
    }
    info = pub.publish(_topic(code, product_key, device_name), json.dumps(body), qos=0)
    info.wait_for_publish(timeout=5)


def _connect_protocol(broker, *, product_key=PRODUCT_KEY, device_name=DEVICE_NAME, **extra):
    cfg = {
        "source_ip": broker.host,
        "source_port": broker.host_port,
        "mqtt_use_tls": False,
        "mqtt_read_timeout": 2.0,
        "scada_product_key": product_key,
        "scada_device_name": device_name,
        **extra,
    }
    proto = SCADAProtocol(cfg)
    assert proto.connect() is True
    return proto


def _wait_and_read(proto, points, settle=0.6):
    time.sleep(settle)
    return proto.read_points(points)


def _by_code(results, code):
    for r in results:
        if r["code"] == code:
            return r
    return None


# ===========================================================================
# 1. 真实网关负载结构:data.propertyValue
# ===========================================================================


class TestGatewayPayloadStructure:
    def test_property_value_float(self, broker, publisher):
        proto = _connect_protocol(broker)
        try:
            _publish_gateway(publisher, "N270400150027", "88.5")
            res = _wait_and_read(
                proto, [{"code": "N270400150027", "data_type": "float",
                          "description": "注射压力实际值"}],
            )
            r = _by_code(res, "N270400150027")
            assert r is not None and r["value"] == 88.5
            assert r["quality"] == "good"
        finally:
            proto.disconnect()

    @pytest.mark.parametrize(
        "raw,data_type,expected",
        [
            ("88.5", "float", 88.5),
            ("42", "int", 42),
            ("1", "bool", True),
            ("0", "bool", False),
            ("running", "string", "running"),
        ],
    )
    def test_property_value_data_types(self, broker, publisher, raw, data_type, expected):
        code = f"PT_{data_type}"
        proto = _connect_protocol(broker)
        try:
            _publish_gateway(publisher, code, raw)
            res = _wait_and_read(proto, [{"code": code, "data_type": data_type}])
            r = _by_code(res, code)
            assert r is not None, f"no reading for code={code} raw={raw!r}"
            assert r["value"] == expected
        finally:
            proto.disconnect()

    def test_property_code_authoritative_over_topic_code(self, broker, publisher):
        """data.propertyCode 优先于话题反解出来的 code(话题模板里 {code} 段的值)。"""
        proto = _connect_protocol(broker)
        try:
            # 话题里的 {code} 段和 propertyCode 不一致时,以 propertyCode 为准。
            body = {
                "data": {
                    "deviceCode": DEVICE_NAME,
                    "propertyCode": "REAL_CODE",
                    "dataType": 2,
                    "propertyValue": "5",
                    "time": str(int(time.time() * 1000)),
                }
            }
            info = publisher.publish(_topic("TOPIC_CODE"), json.dumps(body), qos=0)
            info.wait_for_publish(timeout=5)
            res = _wait_and_read(proto, [{"code": "REAL_CODE", "data_type": "int"}])
            r = _by_code(res, "REAL_CODE")
            assert r is not None and r["value"] == 5
        finally:
            proto.disconnect()


# ===========================================================================
# 2. data.time 毫秒时间戳换算
# ===========================================================================


class TestTimestampConversion:
    def test_ms_timestamp_normalised_to_ns(self, broker, publisher):
        proto = _connect_protocol(broker)
        try:
            ms = 1755653755532  # 真实网关样例(见 SCADAProtocol 模块 docstring)
            _publish_gateway(publisher, "TS_PT", "1", time_ms=str(ms))
            res = _wait_and_read(proto, [{"code": "TS_PT", "data_type": "int"}])
            r = _by_code(res, "TS_PT")
            assert r is not None
            assert r["timestamp"] == ms * 10 ** 6, (
                f"expected {ms * 10**6} (ms->ns), got {r['timestamp']}"
            )
        finally:
            proto.disconnect()

    def test_missing_time_falls_back_to_receipt_time(self, broker, publisher):
        proto = _connect_protocol(broker)
        try:
            before = time.time_ns()
            body = {
                "data": {
                    "deviceCode": DEVICE_NAME,
                    "propertyCode": "NO_TIME_PT",
                    "dataType": 2,
                    "propertyValue": "3",
                    # no "time" key at all
                }
            }
            info = publisher.publish(_topic("NO_TIME_PT"), json.dumps(body), qos=0)
            info.wait_for_publish(timeout=5)
            res = _wait_and_read(proto, [{"code": "NO_TIME_PT", "data_type": "int"}])
            after = time.time_ns()
            r = _by_code(res, "NO_TIME_PT")
            assert r is not None
            assert before <= r["timestamp"] <= after
        finally:
            proto.disconnect()


# ===========================================================================
# 3. dataType 变体(枚举值本身被有意忽略,不管什么值都不影响取值)
# ===========================================================================


class TestDataTypeEnumVariants:
    @pytest.mark.parametrize("data_type_enum", [0, 1, 2, 3, 99])
    def test_ignored_regardless_of_value(self, broker, publisher, data_type_enum):
        code = f"DTE_{data_type_enum}"
        proto = _connect_protocol(broker)
        try:
            _publish_gateway(publisher, code, "10", data_type_enum=data_type_enum)
            res = _wait_and_read(proto, [{"code": code, "data_type": "int"}])
            r = _by_code(res, code)
            assert r is not None and r["value"] == 10
        finally:
            proto.disconnect()


# ===========================================================================
# 4. 话题模板 /sys/{pk}/device/{dn}/thing/property/{code}/post,每测点一话题
# ===========================================================================


class TestTopicTemplate:
    def test_default_template_per_point_topic(self, broker, publisher):
        proto = _connect_protocol(broker)
        try:
            # property 层 # 单订阅(平台只授权到这一层;{code}/post 由正则反解)
            assert proto.subscribe_topic == f"/sys/{PRODUCT_KEY}/device/{DEVICE_NAME}/thing/property/#"
            _publish_gateway(publisher, "CODE_A", "1")
            _publish_gateway(publisher, "CODE_B", "2")
            points = [
                {"code": "CODE_A", "data_type": "int"},
                {"code": "CODE_B", "data_type": "int"},
            ]
            res = _wait_and_read(proto, points)
            assert _by_code(res, "CODE_A")["value"] == 1
            assert _by_code(res, "CODE_B")["value"] == 2
        finally:
            proto.disconnect()

    def test_only_configured_points_pass_through(self, broker, publisher):
        """网关在同一话题命名空间下发的、没在 points 里声明的 code 应被静默忽略。"""
        proto = _connect_protocol(broker)
        try:
            _publish_gateway(publisher, "WANTED", "1")
            _publish_gateway(publisher, "UNWANTED", "2")
            res = _wait_and_read(proto, [{"code": "WANTED", "data_type": "int"}])
            assert len(res) == 1
            assert res[0]["code"] == "WANTED"
        finally:
            proto.disconnect()

    def test_custom_topic_template(self, broker, publisher):
        """自定义话题模板(非默认阿里云 IoT 风格)也能正确订阅 + 反解 code。"""
        custom_template = "plant/{product_key}/{device_name}/{code}"
        proto = _connect_protocol(broker, scada_topic_template=custom_template)
        try:
            body = {
                "data": {
                    "deviceCode": DEVICE_NAME,
                    "propertyCode": "CUSTOM_PT",
                    "dataType": 2,
                    "propertyValue": "7",
                    "time": str(int(time.time() * 1000)),
                }
            }
            topic = f"plant/{PRODUCT_KEY}/{DEVICE_NAME}/CUSTOM_PT"
            info = publisher.publish(topic, json.dumps(body), qos=0)
            info.wait_for_publish(timeout=5)
            res = _wait_and_read(proto, [{"code": "CUSTOM_PT", "data_type": "int"}])
            r = _by_code(res, "CUSTOM_PT")
            assert r is not None and r["value"] == 7
        finally:
            proto.disconnect()

    def test_device_isolation_different_device_name_ignored(self, broker, publisher):
        """不同 device_name 的话题不应该被这个协议实例收到(话题正则不匹配)。"""
        proto = _connect_protocol(broker, device_name=DEVICE_NAME)
        try:
            # 发到另一台设备的话题
            _publish_gateway(publisher, "OTHER_DEV_PT", "1", device_name="OTHER_DEVICE_9999")
            _publish_gateway(publisher, "OTHER_DEV_PT", "2")  # 自己的设备,正常应收
            res = _wait_and_read(proto, [{"code": "OTHER_DEV_PT", "data_type": "int"}])
            assert len(res) == 1
            assert res[0]["value"] == 2
        finally:
            proto.disconnect()


# ===========================================================================
# 5. 旧结构兜底(module docstring 里承诺仍然支持)
# ===========================================================================


class TestLegacyFallbackShapes:
    def test_bare_scalar(self, broker, publisher):
        proto = _connect_protocol(broker)
        try:
            info = publisher.publish(_topic("LEGACY_SCALAR"), json.dumps(9.9), qos=0)
            info.wait_for_publish(timeout=5)
            res = _wait_and_read(proto, [{"code": "LEGACY_SCALAR", "data_type": "float"}])
            r = _by_code(res, "LEGACY_SCALAR")
            assert r is not None and r["value"] == 9.9
        finally:
            proto.disconnect()

    def test_value_field(self, broker, publisher):
        proto = _connect_protocol(broker)
        try:
            info = publisher.publish(
                _topic("LEGACY_VALUE"), json.dumps({"value": 3.3}), qos=0
            )
            info.wait_for_publish(timeout=5)
            res = _wait_and_read(proto, [{"code": "LEGACY_VALUE", "data_type": "float"}])
            r = _by_code(res, "LEGACY_VALUE")
            assert r is not None and r["value"] == 3.3
        finally:
            proto.disconnect()


# ===========================================================================
# 6. 断连收敛(broker 容器 stop/start)
# ===========================================================================


class TestReconnection:
    def test_health_check_false_then_recovers(self, broker):
        proto = _connect_protocol(broker)
        recovered_publisher = None
        try:
            assert proto.health_check() is True

            broker.docker_stop()
            deadline = time.time() + 10
            while proto.health_check() and time.time() < deadline:
                time.sleep(0.2)
            assert proto.health_check() is False
            assert proto.is_connected is False

            broker.docker_start()
            assert proto.connect() is True
            assert proto.health_check() is True

            # The publisher is only a test-data injector.  Create it after the
            # broker restart and wait for CONNACK so this assertion measures
            # SCADAProtocol recovery rather than paho's background reconnect
            # timing for an unrelated, previously disconnected fixture.
            recovered_publisher = mqtt.Client()
            recovered_publisher.connect(broker.host, broker.host_port, keepalive=10)
            recovered_publisher.loop_start()
            _wait_for_publisher_connection(recovered_publisher)
            _publish_gateway(recovered_publisher, "AFTER_RECOVERY", "1")
            res = _wait_and_read(proto, [{"code": "AFTER_RECOVERY", "data_type": "int"}])
            r = _by_code(res, "AFTER_RECOVERY")
            assert r is not None and r["value"] == 1
        finally:
            if recovered_publisher is not None:
                recovered_publisher.loop_stop()
                recovered_publisher.disconnect()
            proto.disconnect()
