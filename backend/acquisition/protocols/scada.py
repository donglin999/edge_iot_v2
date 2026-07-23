"""SCADA (MQTT) protocol for the 中山小家电 injection-molding gateway.

This is a thin specialisation of :class:`~acquisition.protocols.mqtt.MQTTProtocol`.
The gateway publishes one MQTT topic *per measurement point*, following the
Alibaba-Cloud-IoT style pattern::

    /sys/{product_key}/device/{device_name}/thing/property/{code}/post

where ``{code}`` is the per-point measurement code (测点编码). We subscribe once,
substituting the single-level wildcard ``+`` for ``{code}``, then map each
inbound message back to the requested point.

Real payload structure
----------------------
Confirmed against the live gateway — this is the contract, not a guess::

    {"data": {
        "deviceCode":    "AN300400152600059",
        "propertyCode":  "N180100560001",
        "dataType":      2,
        "propertyValue": "2",
        "time":          "1755653755532"
    }}

* ``data.propertyValue`` carries the value, **as a string** (``"2"``). It is
  coerced by the point's declared ``data_type``.
* ``data.propertyCode`` is the authoritative measurement code — preferred over
  the code reversed out of the topic.
* ``data.deviceCode`` is the gateway's own device id (informational: the
  protocol instance already knows which device it is reading).
* ``data.time`` is a **string of milliseconds**; it is normalised to
  nanoseconds by magnitude (see :meth:`SCADAProtocol._to_ns`).
* ``data.dataType`` is an int enum of unknown meaning — deliberately ignored.
* Some gateways double-encode ``data`` as a JSON *string*; that is tolerated.

Older/odd payload shapes (bare scalar, ``value``, ``{code}``, ``params``) remain
supported as fallbacks after the real structure, so legacy feeds keep working.

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
    """MQTT-based SCADA gateway adapter (one topic per measurement point).

    Payloads carry ``{"data": {"propertyCode": ..., "propertyValue": "...",
    "time": "<ms>"}}`` (see the module docstring for the full, confirmed
    structure). Value/code/timestamp are taken from there; the topic-derived
    code and the legacy payload shapes are fallbacks only.
    """

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
        # required=False: base.py's validator only flags a field as "missing"
        # when BOTH required=True and default is None — with a non-empty
        # default set, required=True here was a no-op (never actually
        # enforced) while still drawing a misleading required-field asterisk
        # in the frontend form. This has a sane default, so it isn't required.
        FieldSpec("scada_topic_template", "话题模板", required=False,
                  default=DEFAULT_TOPIC_TEMPLATE,
                  example=DEFAULT_TOPIC_TEMPLATE,
                  help_text="支持 {product_key} {device_name} {code} 占位符;{code} 为逐测点编码;"
                            "留空则使用默认模板"),
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
                  help_text="可选,形如 'data.propertyValue';留空时自动探测:优先网关真实结构 "
                            "data.propertyValue(data 亦可为 JSON 字符串),再兜底 "
                            "标量/value/{code}/params[{code}] 等旧结构"),
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

            raw = message.get("payload")
            try:
                payload = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
            except (json.JSONDecodeError, ValueError, TypeError):
                self.logger.warning("SCADA non-JSON payload on %s, skipping", topic)
                return []

            # The gateway's own propertyCode is authoritative; the topic-derived
            # code is only a fallback for payloads that don't carry one.
            data = self._data_section(payload)
            if isinstance(data, dict):
                prop_code = str(data.get("propertyCode") or "").strip()
                if prop_code:
                    code = prop_code

            point = next(
                (p for p in points if str(p.get("code")) == code), None
            )
            if point is None:
                # Received a code we're not asked to collect.
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
    @staticmethod
    def _data_section(payload: Any) -> Any:
        """Return ``payload["data"]``, decoding it if double-encoded as JSON."""
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        if isinstance(data, (str, bytes, bytearray)):
            try:
                data = json.loads(data)
            except (json.JSONDecodeError, ValueError, TypeError):
                return None
        return data

    def _extract_value(self, payload: Any, code: str, payload_path: str) -> Any:
        """Tolerant value extraction.

        Order (when no explicit ``payload_path``):
        1. ``payload["data"]["propertyValue"]`` — the real gateway structure
           (``data`` may itself be a JSON string)
        2. bare scalar payload            (legacy fallback)
        3. ``payload["value"]``           (legacy fallback)
        4. ``payload[code]``              (legacy fallback)
        5. ``payload["params"][code]["value"]`` or ``payload["params"][code]``
        """
        if payload_path:
            return self._follow_path(payload, payload_path)

        data = self._data_section(payload)
        if isinstance(data, dict) and data.get("propertyValue") is not None:
            return data["propertyValue"]

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
    def _to_ns(raw: Any) -> Optional[int]:
        """Normalise a s/ms/µs/ns timestamp to nanoseconds, by magnitude.

        The gateway sends a *string* of milliseconds (``"1755653755532"``), but
        peers differ, so classify on magnitude rather than trusting a unit:
        ``<1e11`` → s, ``<1e14`` → ms, ``<1e17`` → µs, else already ns.
        Returns ``None`` when unparseable or non-positive, so callers can fall
        back to the receipt time.
        """
        try:
            t = int(raw)
        except (TypeError, ValueError):
            return None
        if t <= 0:
            return None
        if t < 10 ** 11:
            return t * 10 ** 9   # seconds
        if t < 10 ** 14:
            return t * 10 ** 6   # milliseconds  ← what the gateway actually sends
        if t < 10 ** 17:
            return t * 10 ** 3   # microseconds
        return t                 # already nanoseconds

    @classmethod
    def _extract_timestamp(cls, payload: Any, message: Dict[str, Any]) -> int:
        """Prefer a payload-carried timestamp (normalised to ns), else the
        receipt time — which ``MQTTProtocol`` already records via
        ``time.time_ns()``, so it needs no normalisation."""
        data = cls._data_section(payload)
        if isinstance(data, dict) and "time" in data:
            ts = cls._to_ns(data["time"])
            if ts is not None:
                return ts

        if isinstance(payload, dict):
            for key in ("time", "timestamp", "ts"):
                if key in payload:
                    ts = cls._to_ns(payload[key])
                    if ts is not None:
                        return ts

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
