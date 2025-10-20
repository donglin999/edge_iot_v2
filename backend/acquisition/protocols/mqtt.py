"""MQTT protocol implementation for subscription-based data acquisition."""
from __future__ import annotations

import json
import queue
import ssl
import threading
import time
from typing import Any, Dict, List, Optional

import paho.mqtt.client as mqtt

from .base import BaseProtocol, ConnectionError, ProtocolRegistry, ReadError


@ProtocolRegistry.register("mqtt")
class MQTTProtocol(BaseProtocol):
    """
    MQTT protocol adapter for subscription-based data collection.

    Unlike request-response protocols, MQTT uses publish-subscribe pattern.
    """

    def __init__(self, device_config: Dict[str, Any]) -> None:
        super().__init__(device_config)
        self.broker_ip = device_config.get("source_ip")
        self.broker_port = device_config.get("source_port", 1883)
        self.username = device_config.get("mqtt_username")
        self.password = device_config.get("mqtt_password")
        self.use_tls = device_config.get("mqtt_use_tls", False)
        self.protocol_version = device_config.get("mqtt_protocol", mqtt.MQTTv5)

        # Topics to subscribe
        self.topics = device_config.get("mqtt_topics", [])
        if not isinstance(self.topics, list):
            self.topics = [self.topics]

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
        timeout = 5  # seconds

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
