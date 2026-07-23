"""MQTT protocol implementation for subscription-based data acquisition."""
from __future__ import annotations

import json
import queue
import ssl
import threading
import time
from typing import Any, Dict, List, Optional

import paho.mqtt.client as mqtt

from .base import (
    BaseProtocol,
    ConnectionError,
    FieldSpec,
    ProtocolMeta,
    ProtocolRegistry,
    ReadError,
)


@ProtocolRegistry.register("mqtt")
class MQTTProtocol(BaseProtocol):
    """
    MQTT protocol adapter for subscription-based data collection.

    Unlike request-response protocols, MQTT uses publish-subscribe pattern.
    """

    META = ProtocolMeta(
        name="mqtt",
        label="MQTT",
        category="iot",
        description="发布订阅型协议,常用于云端/边缘 IoT 网关。需要预先订阅话题,采集循环从消息队列拉数据。",
        supports_pause=True,
    )

    DEVICE_FIELDS = (
        FieldSpec("source_ip", "Broker 地址", required=True, example="broker.example.com"),
        FieldSpec("source_port", "Broker 端口", kind="int", default=1883, example=1883),
        FieldSpec("mqtt_topics", "订阅话题", required=True,
                  help_text="多个话题用逗号或分号分隔,支持 + 与 # 通配", example="sensor/+/temp"),
        FieldSpec("mqtt_qos", "QoS", kind="enum", choices=(0, 1, 2), default=0),
        FieldSpec("mqtt_username", "用户名", default=""),
        FieldSpec("mqtt_password", "密码", kind="secret", default=""),
        FieldSpec("mqtt_use_tls", "启用 TLS", kind="bool", default=False),
        FieldSpec("mqtt_client_id", "ClientID", default="",
                  help_text="留空自动生成"),
        FieldSpec("mqtt_read_timeout", "读取超时(秒)", kind="float", default=5.0,
                  help_text="采集循环从消息队列拉数据时的最长等待时间;队列空闲超过该值即结束本轮读取"),
    )
    # mqtt_client_id defaults to "" (auto-generated at connect time), so it
    # must not be part of device identity: two devices on the same broker
    # that both leave ClientID blank would collapse to the same identity
    # tuple (ip, port, "") and get de-duplicated into one device on import.
    # mqtt_topics is what actually distinguishes independent subscriptions on
    # the same broker (mirrors the scada.py product_key+device_name pattern).
    IDENTITY_FIELDS = ("source_ip", "source_port", "mqtt_topics")

    POINT_FIELDS = (
        FieldSpec("code", "测点编码", required=True,
                  help_text="若 payload 为 JSON,该 code 即字段名;若 payload 为标量,所有点 code 都收同一个值",
                  example="temperature"),
        FieldSpec("topic_filter", "话题过滤", default="",
                  help_text="可选,只接收指定话题的消息(支持通配)"),
        FieldSpec("payload_path", "JSON 路径", default="",
                  help_text="形如 'data.value';留空时按 code 取顶层字段"),
        FieldSpec("data_type", "数据类型", kind="enum",
                  choices=("string", "int", "float", "bool"), default="float"),
        FieldSpec("unit", "单位", default=""),
        FieldSpec("description", "中文名称", default=""),
    )

    def __init__(self, device_config: Dict[str, Any]) -> None:
        super().__init__(device_config)
        self.broker_ip = device_config.get("source_ip")
        self.broker_port = int(device_config.get("source_port", 1883))
        self.username = device_config.get("mqtt_username")
        self.password = device_config.get("mqtt_password")
        self.use_tls = bool(device_config.get("mqtt_use_tls", False))
        self.client_id = device_config.get("mqtt_client_id", "") or None
        self.protocol_version = device_config.get("mqtt_protocol", mqtt.MQTTv5)
        self.qos = int(device_config.get("mqtt_qos", 0))

        topics = device_config.get("mqtt_topics", [])
        if isinstance(topics, str):
            # accept comma- or semicolon-separated topic lists
            topics = [t.strip() for t in topics.replace(";", ",").split(",") if t.strip()]
        self.topics = topics

        # How long read_points() waits on an empty queue before returning.
        try:
            self.read_timeout = float(device_config.get("mqtt_read_timeout", 5.0))
        except (TypeError, ValueError):
            self.read_timeout = 5.0
        if self.read_timeout <= 0:
            self.read_timeout = 5.0

        self.client: Optional[mqtt.Client] = None
        self.data_queue = queue.Queue(maxsize=1000)
        self.is_running = False

        # Set by the on_connect wrapper installed in connect(); used to block
        # briefly for the broker's CONNACK before reporting success (see the
        # comment in connect() for why this matters).
        self._connect_event: Optional[threading.Event] = None
        self._connect_rc: Optional[int] = None

    def connect(self) -> bool:
        """Connect to MQTT broker and subscribe to topics."""
        # Guard against a stray live client from a previous connect() call on
        # this same instance (e.g. an explicit reconnect after a broker
        # outage). Without this, the old client's background network thread
        # keeps running and its callbacks — bound to this same protocol
        # instance — can fire after the new client's, flipping is_connected
        # back and forth unpredictably (e.g. old client's delayed
        # auto-reconnect succeeding, then losing the connection again, right
        # after the new client already reported success).
        if self.client is not None:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self.client = None

        try:
            self.client = mqtt.Client(protocol=self.protocol_version)

            # Set authentication
            if self.username and self.password:
                self.client.username_pw_set(self.username, self.password)

            # Set TLS if required
            if self.use_tls:
                context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
                self.client.tls_set_context(context)

            # Set callbacks. on_connect is wrapped (rather than assigned
            # self._on_connect directly) so that subclasses which override
            # _on_connect for their own subscription logic (e.g. SCADAProtocol)
            # still get the CONNACK-confirmation signalling below for free.
            self._connect_event = threading.Event()
            self._connect_rc = None

            def _on_connect_wrapper(client, userdata, flags, rc, properties=None):
                self._connect_rc = rc
                try:
                    self._on_connect(client, userdata, flags, rc, properties)
                finally:
                    self._connect_event.set()

            self.client.on_connect = _on_connect_wrapper
            self.client.on_message = self._on_message
            self.client.on_disconnect = self._on_disconnect

            # Track SUBSCRIBE acknowledgements too (see the note near
            # confirm_timeout below for why: CONNACK alone isn't enough).
            # client.subscribe is intercepted here — rather than only tracking
            # calls made from _on_connect directly — so it also covers
            # SCADAProtocol's overridden _on_connect without touching scada.py.
            pending_sub_mids: set = set()
            subs_done = threading.Event()
            _orig_subscribe = self.client.subscribe

            def _tracked_subscribe(topic, qos=0):
                result = _orig_subscribe(topic, qos)
                try:
                    _, mid = result
                    pending_sub_mids.add(mid)
                except (TypeError, ValueError):
                    pass
                return result

            self.client.subscribe = _tracked_subscribe

            def _on_subscribe(client, userdata, mid, granted_qos, properties=None):
                pending_sub_mids.discard(mid)
                if not pending_sub_mids:
                    subs_done.set()

            self.client.on_subscribe = _on_subscribe

            # paho's connect() only does the TCP handshake and sends the
            # CONNECT packet; it does NOT wait for the broker's CONNACK. That
            # is processed asynchronously by the network thread started by
            # loop_start(), which is when on_connect actually fires and
            # subscriptions get sent.
            self.client.connect(self.broker_ip, self.broker_port, keepalive=60)
            self.client.loop_start()

            # Block briefly for that CONNACK confirmation instead of
            # declaring victory on the socket connect alone. Without this,
            # is_connected/health_check() report success before the broker
            # has actually acknowledged the session — callers that immediately
            # follow connect() with health_check() (e.g. the device
            # connection-test diagnostic in connection_trace.py) would race
            # the background thread and almost always see a false "connected
            # but unhealthy" on the very first check.
            confirm_timeout = max(3.0, min(self.read_timeout, 10.0))
            if not self._connect_event.wait(confirm_timeout):
                self.is_connected = False
                msg = (
                    f"MQTT connect to {self.broker_ip}:{self.broker_port} timed out "
                    f"waiting for CONNACK after {confirm_timeout}s"
                )
                self.logger.error(msg)
                raise ConnectionError(msg)
            if self._connect_rc != 0:
                self.is_connected = False
                msg = f"MQTT broker rejected connection, rc={self._connect_rc}"
                self.logger.error(msg)
                raise ConnectionError(msg)

            # CONNACK only confirms the session — the SUBSCRIBE requests sent
            # from _on_connect are themselves acknowledged asynchronously
            # (SUBACK), also via the network thread. A message published right
            # after connect() returns can otherwise be silently missed forever
            # (non-retained messages aren't redelivered) if the SUBACK hasn't
            # landed yet. Best-effort: wait briefly, but don't fail connect()
            # over it — a broker that never SUBACKs is unusual enough that
            # refusing to proceed would be the wrong tradeoff.
            if pending_sub_mids and not subs_done.wait(max(2.0, min(self.read_timeout, 5.0))):
                self.logger.warning(
                    "MQTT SUBSCRIBE not fully acknowledged before connect() returned "
                    "(topics=%s) — messages published in this window may be missed",
                    self.topics,
                )

            self.is_connected = True
            self.is_running = True
            self.logger.info(f"Connected to MQTT broker {self.broker_ip}:{self.broker_port}")
            return True

        except ConnectionError:
            raise
        except Exception as e:
            self.is_connected = False
            self.logger.error(f"Failed to connect to MQTT broker: {e}")
            raise ConnectionError(f"MQTT connection failed: {e}") from e

    def disconnect(self) -> None:
        """Disconnect from MQTT broker."""
        self.is_running = False
        if self.client:
            try:
                self.client.loop_stop()
                self.client.disconnect()
                self.is_connected = False
                self.logger.info(f"Disconnected from MQTT broker")
            except Exception as e:
                self.logger.warning(f"Error during disconnect: {e}")
            finally:
                self.client = None

    def read_points(self, points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Read data from MQTT message queue.

        For MQTT, this doesn't actively request data but retrieves
        messages that were received via subscriptions.

        Args:
            points: List of point configurations (used for filtering/mapping)

        Returns:
            List of data readings from the queue.
        """
        if not self.is_connected:
            if not self.connect():
                raise ReadError("Not connected to MQTT broker")

        results = []
        timeout = self.read_timeout  # seconds, configurable via mqtt_read_timeout

        try:
            # Try to get messages from queue with timeout
            while True:
                try:
                    message = self.data_queue.get(timeout=timeout)
                    # Parse message and create result
                    result = self._parse_message(message, points)
                    if result:
                        results.extend(result)
                except queue.Empty:
                    break
        except Exception as e:
            self.logger.error(f"Error reading from MQTT queue: {e}")
            raise ReadError(f"MQTT read error: {e}") from e

        return results

    def health_check(self) -> bool:
        """Check if MQTT connection is alive."""
        if not self.is_connected or not self.client:
            return False
        return self.client.is_connected()

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        """Callback when connected to broker."""
        if rc == 0:
            self.logger.info(f"MQTT connected successfully")
            # Subscribe to all configured topics
            for topic in self.topics:
                client.subscribe(topic)
                self.logger.info(f"Subscribed to topic: {topic}")
        else:
            self.logger.error(f"MQTT connection failed with code: {rc}")

    def _on_message(self, client, userdata, msg):
        """Callback when message is received."""
        try:
            # Store raw message in queue
            message_data = {
                "topic": msg.topic,
                "payload": msg.payload.decode("utf-8"),
                "timestamp": time.time_ns(),
                "qos": msg.qos,
            }
            self.data_queue.put(message_data, block=False)
        except queue.Full:
            self.logger.warning("Message queue is full, dropping message")
        except Exception as e:
            self.logger.error(f"Error processing MQTT message: {e}")

    def _on_disconnect(self, client, userdata, rc, properties=None):
        """Callback when disconnected from broker."""
        self.logger.warning(f"MQTT disconnected with code: {rc}")
        self.is_connected = False

    #: Sentinel distinguishing "field not found" from "field found, value None".
    _MISSING = object()

    def _parse_message(
        self, message: Dict[str, Any], points: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Parse MQTT message and map to point readings.

        Per point (in declared POINT_FIELDS order):
        1. ``topic_filter`` — if set, the message's topic must match it
           (MQTT wildcard semantics) or the point is skipped for this message.
        2. ``payload_path`` — if set, follow the dotted path (e.g.
           ``"data.value"``) into the parsed JSON payload.
        3. Otherwise: JSON object payload → ``payload[code]``, falling back to
           ``payload["value"]``; non-JSON/scalar payload → the raw payload
           applied to every matching point.
        4. The resolved value is coerced to the point's declared ``data_type``.

        Args:
            message: Raw message from queue
            points: Point configurations for mapping

        Returns:
            List of parsed readings.
        """
        results = []
        topic = message.get("topic", "") or ""
        raw_payload = message["payload"]

        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError:
            payload = None  # not JSON — handled as a raw scalar/string below

        for point in points:
            try:
                point_code = point["code"]
                topic_filter = point.get("topic_filter") or ""
                if topic_filter and not mqtt.topic_matches_sub(topic_filter, topic):
                    continue

                payload_path = point.get("payload_path") or ""
                value = self._MISSING
                if payload_path:
                    resolved = self._follow_path(
                        payload if payload is not None else raw_payload, payload_path
                    )
                    if resolved is not None:
                        value = resolved
                elif payload is None:
                    # Non-JSON payload: treat as a raw string/scalar shared by
                    # every point whose topic_filter (if any) matched above.
                    value = raw_payload
                elif isinstance(payload, dict):
                    if point_code in payload:
                        value = payload[point_code]
                    elif "value" in payload:
                        value = payload["value"]
                else:
                    # Bare JSON scalar (number/bool/string/list).
                    value = payload

                if value is self._MISSING:
                    continue

                results.append({
                    "code": point_code,
                    "value": self._coerce_value(value, point.get("data_type", "float")),
                    "timestamp": message["timestamp"],
                    "quality": "good",
                    "topic": topic,
                })
            except Exception as e:
                self.logger.error(f"Error parsing MQTT message for point {point.get('code')}: {e}")

        return results

    @staticmethod
    def _follow_path(payload: Any, path: str) -> Any:
        """Follow a dotted path (dict keys, list indices) into ``payload``.

        ``payload`` may be a decoded JSON value or a raw string; a raw string
        only resolves a path that immediately fails, returning ``None``.
        """
        cur = payload
        for part in path.split("."):
            if not part:
                continue
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            elif isinstance(cur, list) and part.lstrip("-").isdigit():
                idx = int(part)
                if -len(cur) <= idx < len(cur):
                    cur = cur[idx]
                else:
                    return None
            else:
                return None
        return cur

    @staticmethod
    def _coerce_value(value: Any, data_type: str) -> Any:
        """Best-effort coercion by declared data_type; leave as-is on failure."""
        try:
            if data_type == "int":
                return int(float(value))
            if data_type == "float":
                return float(value)
            if data_type == "bool":
                if isinstance(value, str):
                    return value.strip().lower() in ("1", "true", "yes", "y", "on")
                return bool(value)
            if data_type == "string":
                return str(value)
        except (TypeError, ValueError):
            return value
        return value
