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
    IDENTITY_FIELDS = ("source_ip", "source_port", "mqtt_client_id")

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

    def connect(self) -> bool:
        """Connect to MQTT broker and subscribe to topics."""
        try:
            self.client = mqtt.Client(protocol=self.protocol_version)

            # Set authentication
            if self.username and self.password:
                self.client.username_pw_set(self.username, self.password)

            # Set TLS if required
            if self.use_tls:
                context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
                self.client.tls_set_context(context)

            # Set callbacks
            self.client.on_connect = self._on_connect
            self.client.on_message = self._on_message
            self.client.on_disconnect = self._on_disconnect

            # Connect
            self.client.connect(self.broker_ip, self.broker_port, keepalive=60)
            self.client.loop_start()

            self.is_connected = True
            self.is_running = True
            self.logger.info(f"Connected to MQTT broker {self.broker_ip}:{self.broker_port}")
            return True

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

    def _parse_message(
        self, message: Dict[str, Any], points: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Parse MQTT message and map to point readings.

        Args:
            message: Raw message from queue
            points: Point configurations for mapping

        Returns:
            List of parsed readings.
        """
        results = []

        try:
            # Try to parse payload as JSON
            payload = json.loads(message["payload"])

            # If payload is a dict, create readings for matching points
            if isinstance(payload, dict):
                for point in points:
                    point_code = point["code"]
                    if point_code in payload:
                        results.append({
                            "code": point_code,
                            "value": payload[point_code],
                            "timestamp": message["timestamp"],
                            "quality": "good",
                            "topic": message["topic"],
                        })
            else:
                # If not a dict, create single reading
                if points:
                    results.append({
                        "code": points[0]["code"],
                        "value": payload,
                        "timestamp": message["timestamp"],
                        "quality": "good",
                        "topic": message["topic"],
                    })

        except json.JSONDecodeError:
            # If not JSON, treat as string
            if points:
                results.append({
                    "code": points[0]["code"],
                    "value": message["payload"],
                    "timestamp": message["timestamp"],
                    "quality": "good",
                    "topic": message["topic"],
                })
        except Exception as e:
            self.logger.error(f"Error parsing MQTT message: {e}")

        return results
