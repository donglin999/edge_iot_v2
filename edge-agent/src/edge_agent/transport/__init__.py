"""Pluggable uplink transports for the edge-agent (Phase 2 P3 — XIU-102).

The M5 edge-agent piped all uplink frames (``lifecycle`` / ``sample_batch``
/ ``alarm_event``) straight onto the long-lived WebSocket to the center.
Phase 2 (plan §9) replaces the edge ↔ center WS with an MQTT bus, one
direction at a time:

* P1 (XIU-100) brought up Mosquitto + the center-side subscriber skeleton.
* P3 (this milestone) switches the **edge → center uplink** to MQTT
  publish at QoS 1. The seq'd outbox stays unchanged; only the wire egress
  changes.

The classes here are the swap point:

* :class:`Transport` — abstract interface with the three publish methods
  called out in the XIU-102 scope (``send_state`` / ``send_sample`` /
  ``send_alarm``), plus a generic ``publish(frame)`` dispatcher used by
  the outbox uplink loop.
* :class:`WsTransport` — adapter that wraps the existing per-session
  WebSocket connection, so the WS path keeps shipping uplink the way it
  did in M5 when an operator opts back in (``EDGE_TRANSPORT=ws``).
* :class:`MqttTransport` — aiomqtt-backed implementation that publishes
  each frame to ``edge/<edge_id>/uplink/<frame_type>`` at QoS 1. Lives in
  the sibling :mod:`mqtt_client` module.
"""
from __future__ import annotations

from .base import Transport
from .mqtt_client import MqttTransport
from .ws_transport import WsTransport

__all__ = ["Transport", "WsTransport", "MqttTransport"]
