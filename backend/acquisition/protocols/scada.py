"""SCADA (MQTT) protocol for the 中山小家电 injection-molding gateway.

This is a thin specialisation of :class:`~acquisition.protocols.mqtt.MQTTProtocol`.
The gateway publishes one MQTT topic *per measurement point*, following the
Alibaba-Cloud-IoT style pattern::

    /sys/{product_key}/device/{device_name}/thing/property/{code}/post

where ``{code}`` is the per-point measurement code (测点编码). We subscribe once,
substituting the single-level wildcard ``+`` for ``{code}``, then recover the
concrete ``code`` from each inbound message's topic and map it back to the
requested point.

Connection/queue/loop plumbing (TLS, username/password, bounded queue, draining
in ``read_points``) is entirely inherited from :class:`MQTTProtocol`; only topic
construction and payload parsing are specialised here.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

from .base import FieldSpec, ProtocolMeta, ProtocolRegistry
from .mqtt import MQTTProtocol

#: Default topic template. ``{product_key}``/``{device_name}`` are substituted
#: from device config; ``{code}`` is the per-point measurement code.
DEFAULT_TOPIC_TEMPLATE = (
    "/sys/{product_key}/device/{device_name}/thing/property/{code}/post"
)

#: Sentinel used while turning the template into a regex, so we can escape the
#: literal parts of the template without escaping our capture-group marker.
_CODE_SENTINEL = "\x00__CODE__\x00"


@ProtocolRegistry.register("scada")
class SCADAProtocol(MQTTProtocol):
    """MQTT-based SCADA gateway adapter (one topic per measurement point)."""

    META = ProtocolMeta(
        name="scada",
        label="SCADA 网关 (MQTT)",
        category="iot",
        description=(
            "面向注塑机/小家电 SCADA 网关的 MQTT 采集协议:每个测点一个话题,"
            "按 /sys/{product_key}/device/{device_name}/thing/property/{code}/post "
            "模式订阅(用 + 通配 {code}),从话题反解测点编码并写入时序库。"
        ),
        supports_pause=True,
    )

    DEVICE_FIELDS = (
        # --- connection / identity plumbing (names shared with MQTTProtocol so
        #     the existing pipeline keeps reading them unchanged) ---
        FieldSpec("source_ip", "Broker 地址", required=True, example="10.134.14.147"),
        FieldSpec("source_port", "Broker 端口", kind="int", default=8883, example=8883,
                  help_text="8883 通常为 MQTT-over-TLS 端口"),
        FieldSpec("mqtt_username", "用户名", default="", example="ZYY_XJDZS"),
        FieldSpec("mqtt_password", "密码", kind="secret", default="",
                  example="<在前端配置时填写>",
                  help_text="运行时在前端配置,切勿写入代码/模板"),
        FieldSpec("mqtt_use_tls", "启用 TLS", kind="bool", default=True, example=True,
                  help_text="8883 端口默认开启 TLS"),
        FieldSpec("mqtt_qos", "QoS", kind="enum", choices=(0, 1, 2), default=0),
        FieldSpec("mqtt_client_id", "ClientID", default="", help_text="留空自动生成"),
        FieldSpec("mqtt_read_timeout", "读取超时(秒)", kind="float", default=5.0,
                  help_text="采集循环从消息队列拉数据时的最长等待时间;队列空闲超过该值即结束本轮读取"),
        # --- scada-specific ---
        FieldSpec("scada_product_key", "产品 Key", required=True,
                  example="123daffb91264286adcdf3bfe55194c7",
                  help_text="话题中的 {product_key} 段"),
        FieldSpec("scada_device_name", "设备名", required=True,
                  example="A0201010001150403",
                  help_text="话题中的 {device_name} 段"),
        FieldSpec("scada_topic_template", "话题模板", required=True,
                  default=DEFAULT_TOPIC_TEMPLATE,
                  example=DEFAULT_TOPIC_TEMPLATE,
                  help_text="支持 {product_key} {device_name} {code} 占位符;{code} 为逐测点编码"),
    )
    IDENTITY_FIELDS = ("source_ip", "source_port", "scada_product_key", "scada_device_name")

    POINT_FIELDS = (
        FieldSpec("code", "测点编码", required=True, example="N270400150027",
                  help_text="话题中 {code} 段的取值,如 N270400150027(注射压力实际值)"),
        FieldSpec("data_type", "数据类型", kind="enum",
                  choices=("string", "int", "float", "bool"), default="float"),
        FieldSpec("unit", "单位", default=""),
        FieldSpec("description", "中文名称", default="", example="注射压力实际值"),
        FieldSpec("payload_path", "JSON 路径", default="",
                  help_text="可选,形如 'params.value';留空时自动探测 value/{code}/params[{code}] 等常见结构"),
    )

    # ------------------------------------------------------------------ #
    def __init__(self, device_config: Dict[str, Any]) -> None:
        super().__init__(device_config)

        # Protocol-specific defaults (belt-and-braces for direct instantiation
        # that bypasses ProtocolRegistry.create's FieldSpec coercion).
        if "source_port" not in device_config:
            self.broker_port = 8883
        if "mqtt_use_tls" not in device_config:
            self.use_tls = True

        self.product_key = str(device_config.get("scada_product_key", "") or "")
        self.device_name = str(device_config.get("scada_device_name", "") or "")
        self.topic_template = (
            str(device_config.get("scada_topic_template") or DEFAULT_TOPIC_TEMPLATE)
        )

        # Concrete subscribe topic (wildcard for {code}) + regex to reverse it.
        self.subscribe_topic = self._build_subscribe_topic()
        # Override MQTTProtocol's mqtt_topics-derived list: SCADA subscribes to
        # exactly this one wildcard topic.
        self.topics = [self.subscribe_topic] if self.subscribe_topic else []
        self._topic_regex = self._build_topic_regex()

    # ------------------------------------------------------------------ #
    # Topic construction / reversal
    # ------------------------------------------------------------------ #
    def _build_subscribe_topic(self) -> str:
        """Concrete subscribe topic: substitute identity, wildcard the code."""
        if not self.topic_template:
            return ""
        return (
            self.topic_template
            .replace("{product_key}", self.product_key)
            .replace("{device_name}", self.device_name)
            .replace("{code}", "+")
        )

    def _build_topic_regex(self) -> "re.Pattern[str]":
        """Compile a regex where ``{code}`` is a capture group and the identity
        placeholders are their configured values."""
        tmpl = self.topic_template or DEFAULT_TOPIC_TEMPLATE
        tmp = tmpl.replace("{code}", _CODE_SENTINEL)
        tmp = tmp.replace("{product_key}", self.product_key)
        tmp = tmp.replace("{device_name}", self.device_name)
        pattern = re.escape(tmp).replace(re.escape(_CODE_SENTINEL), r"(?P<code>[^/]+)")
        return re.compile("^" + pattern + "$")

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        """Subscribe once to the wildcard topic with the configured QoS."""
        if rc == 0:
            for topic in self.topics:
                client.subscribe(topic, qos=self.qos)
                self.logger.info("SCADA subscribed to topic: %s", topic)
        else:
            self.logger.error("SCADA MQTT connection failed with code: %s", rc)

    # ------------------------------------------------------------------ #
    # Parsing
    # ------------------------------------------------------------------ #
    def _parse_message(
        self, message: Dict[str, Any], points: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Map one queued MQTT message to a single point reading (or nothing).

        Never raises: malformed payloads / non-matching topics are logged and
        skipped so the ``read_points`` drain loop keeps going.
        """
        try:
            topic = message.get("topic", "") or ""
            match = self._topic_regex.match(topic)
            if not match:
                # Topic doesn't belong to this device/template — ignore quietly.
                return []
            code = match.group("code")

            point = next(
                (p for p in points if str(p.get("code")) == code), None
            )
            if point is None:
                # Received a topic we're not asked to collect.
                return []

            raw = message.get("payload")
            try:
                payload = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
            except (json.JSONDecodeError, ValueError, TypeError):
                self.logger.warning("SCADA non-JSON payload on %s, skipping", topic)
                return []

            value = self._extract_value(payload, code, point.get("payload_path", ""))
            if value is None:
                self.logger.debug("SCADA no value extracted for %s on %s", code, topic)
                return []

            value = self._coerce_value(value, point.get("data_type", "float"))
            ts = self._extract_timestamp(payload, message)

            return [{
                "code": code,
                "value": value,
                "timestamp": ts,
                "quality": "good",
                "topic": topic,
            }]
        except Exception as exc:  # noqa: BLE001 — never break the drain loop
            self.logger.error("SCADA parse error on %r: %s", message.get("topic"), exc)
            return []

    # -- value extraction ------------------------------------------------ #
    def _extract_value(self, payload: Any, code: str, payload_path: str) -> Any:
        """Tolerant value extraction.

        Order (when no explicit ``payload_path``):
        1. bare scalar payload
        2. ``payload["value"]``
        3. ``payload[code]``
        4. ``payload["params"][code]["value"]`` or ``payload["params"][code]``
        """
        if payload_path:
            return self._follow_path(payload, payload_path)

        if isinstance(payload, (int, float, str, bool)):
            return payload

        if isinstance(payload, dict):
            if "value" in payload:
                return payload["value"]
            if code in payload:
                return payload[code]
            params = payload.get("params")
            if isinstance(params, dict) and code in params:
                entry = params[code]
                if isinstance(entry, dict) and "value" in entry:
                    return entry["value"]
                return entry
        return None

    @staticmethod
    def _follow_path(payload: Any, path: str) -> Any:
        """Follow a dotted path; supports dict keys and list indices."""
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

    # -- timestamp ------------------------------------------------------- #
    @staticmethod
    def _extract_timestamp(payload: Any, message: Dict[str, Any]) -> int:
        """Prefer a payload-carried timestamp, else the receipt time (ns)."""
        if isinstance(payload, dict):
            for key in ("time", "timestamp", "ts"):
                if key in payload:
                    try:
                        return int(payload[key])
                    except (TypeError, ValueError):
                        pass
        recv = message.get("timestamp")
        if recv is not None:
            try:
                return int(recv)
            except (TypeError, ValueError):
                pass
        return time.time_ns()

    # -- coercion -------------------------------------------------------- #
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
