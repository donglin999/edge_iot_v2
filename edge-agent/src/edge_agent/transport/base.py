"""Abstract uplink transport interface (Phase 2 P3 — XIU-102).

The seq'd outbox (``edge_agent.outbox.DurableOutbox``) is the durability
layer: it allocates a monotonic seq, builds the frame, and persists it to
SQLite *before* it ever hits the wire. The transport sits one layer above
that and is responsible only for putting the bytes on the chosen channel
(WebSocket or MQTT) and reporting back whether the channel has confirmed
delivery so the outbox can be pruned.

Concrete implementations:

* :class:`~edge_agent.transport.ws_transport.WsTransport` — wraps a per-session
  ``websockets.WebSocketClientProtocol``. ``confirms_on_publish`` is
  ``False`` because the center later sends an explicit ``ack`` carrying
  ``last_uplink_seq`` over the same WS, which is what drives the outbox
  prune (see ``EdgeAgent._handle_ack``).
* :class:`~edge_agent.transport.mqtt_client.MqttTransport` — uses aiomqtt
  to publish at QoS 1 to ``edge/<edge_id>/uplink/<frame_type>``.
  ``confirms_on_publish`` is ``True`` because :meth:`aiomqtt.Client.publish`
  blocks until the broker has acked the PUBLISH (PUBACK at QoS 1) — the
  outbox row can be pruned the moment publish returns. In MQTT mode the
  center-side ack-over-WS path is unused (P4 will revisit prune semantics
  when last-will / outbox reconnect over MQTT lands).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict


class Transport(ABC):
    """One direction of edge ↔ center traffic for the seq'd uplink stream."""

    # Whether a successful ``send_*`` call constitutes delivery confirmation.
    # WS: False — the center later sends ``ack {last_uplink_seq}`` over WS.
    # MQTT QoS 1: True — broker PUBACK = delivered to broker, broker takes
    # over from there.
    confirms_on_publish: bool = False

    @abstractmethod
    async def connect(self) -> None:
        """Bring the transport up. Idempotent on already-connected impls."""

    @abstractmethod
    async def close(self) -> None:
        """Tear the transport down. Idempotent."""

    @abstractmethod
    async def send_state(self, frame: Dict[str, Any]) -> None:
        """Publish a v0.3 ``lifecycle`` frame."""

    @abstractmethod
    async def send_sample(self, frame: Dict[str, Any]) -> None:
        """Publish a v0.3 ``sample_batch`` frame."""

    @abstractmethod
    async def send_alarm(self, frame: Dict[str, Any]) -> None:
        """Publish a v0.4 ``alarm_event`` frame."""

    async def publish(self, frame: Dict[str, Any]) -> None:
        """Dispatch by frame type — convenience for the uplink loop.

        The outbox can hold any of the three uplink frame types; this
        helper saves the loop from a chain of ``isinstance`` checks. An
        unsupported frame type raises :class:`ValueError` so a protocol
        bug surfaces loudly rather than being silently dropped.
        """
        frame_type = frame.get("type")
        if frame_type == "lifecycle":
            await self.send_state(frame)
        elif frame_type == "sample_batch":
            await self.send_sample(frame)
        elif frame_type == "alarm_event":
            await self.send_alarm(frame)
        else:
            raise ValueError(f"transport: unsupported uplink frame type {frame_type!r}")
