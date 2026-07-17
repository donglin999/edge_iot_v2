"""Single source of truth for assembling a protocol's ``device_config``.

Every place that instantiates a protocol (``ProtocolRegistry.create``) must
build its config through :func:`build_device_config`. Before this module the
dict was open-coded in four places, which meant a device attribute that lives
outside ``Device.metadata`` — such as the new ``Device.gateway`` — would only
be honoured wherever someone remembered to add it.

Layering (later wins):

1. **Base** — ``source_ip`` / ``source_port`` / ``protocol_type`` off the
   ``Device`` row. Unchanged from the historical open-coded shape.
2. **Gateway overlay** — only when ``device.gateway`` is set: the shared MQTT
   connection block, re-keyed onto the names ``SCADAProtocol`` reads
   (``product_key`` -> ``scada_product_key``, ...).
3. **Device metadata** — stays last so a single device can still carry a
   device-specific exception to a gateway value (and so non-gateway devices
   are byte-for-byte identical to the old behaviour).
4. **Caller overrides** — e.g. the pipeline's auto-derived ``timeout``, which
   must beat a user-supplied metadata value.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def build_device_config(device, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Assemble the full protocol config for ``device``.

    Args:
        device: A ``configuration.models.Device`` (or any duck-typed stand-in
            exposing ``ip_address``, ``port``, ``protocol``, ``metadata`` and
            optionally ``gateway``).
        overrides: Keys applied *after* device metadata. Used by callers that
            must win over user-supplied config — the continuous pipeline's
            auto-derived ``timeout``, the one-shot probe's fail-fast timeout.

    Returns:
        The ``device_config`` dict to hand to ``ProtocolRegistry.create``.
    """
    cfg: Dict[str, Any] = {
        "source_ip": device.ip_address,
        "source_port": device.port,
        "protocol_type": device.protocol,
    }

    # ``getattr`` rather than ``device.gateway`` so duck-typed test stand-ins
    # (and any non-Device caller) keep working without declaring the field.
    gateway = getattr(device, "gateway", None)
    if gateway is not None:
        cfg.update(_gateway_overlay(gateway))

    cfg.update(device.metadata or {})

    if overrides:
        cfg.update(overrides)

    return cfg


def _gateway_overlay(gateway) -> Dict[str, Any]:
    """Map a :class:`~configuration.models.ScadaGateway` onto protocol keys.

    The gateway model uses un-prefixed field names (``product_key``); the
    protocol's ``DEVICE_FIELDS`` declare the ``scada_``-prefixed ones. This is
    the only place that translation happens.

    ``scada_device_name`` is deliberately absent — it is the one connection key
    that genuinely varies per device, so it comes from ``device.metadata``.
    """
    return {
        "source_ip": gateway.source_ip,
        "source_port": gateway.source_port,
        "mqtt_use_tls": gateway.mqtt_use_tls,
        "mqtt_username": gateway.mqtt_username,
        "mqtt_password": gateway.mqtt_password,
        "mqtt_qos": gateway.mqtt_qos,
        "mqtt_client_id": gateway.mqtt_client_id,
        "mqtt_read_timeout": gateway.mqtt_read_timeout,
        "scada_product_key": gateway.product_key,
        "scada_topic_template": gateway.topic_template,
    }
