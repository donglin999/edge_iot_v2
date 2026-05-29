"""aiomqtt-backed uplink transport (Phase 2 P3 — XIU-102, P4 — XIU-103).

Publishes the seq'd uplink frames (lifecycle / sample_batch / alarm_event)
to the center MQTT broker stood up by Phase 2 P1 (XIU-100). Topic layout
mirrors the center subscriber's expectations
(:mod:`backend.fleet.mqtt_transport`):

* ``edge/<edge_id>/uplink/lifecycle``
* ``edge/<edge_id>/uplink/sample_batch``
* ``edge/<edge_id>/uplink/alarm_event``
* ``edge/<edge_id>/lwt``               (P4 — last-will + retained online)

All publishes go at QoS 1. The center subscriber wildcards on
``edge/+/uplink/#`` at the same QoS so the per-message PUBACK / PUBREL
flow is in place end-to-end. Combined with the existing client-side
``monotonic_seq`` dedup on the center, that gives us the
"at-least-once + dedup = exactly-once" semantics called out in the issue
("QoS 1 + seq 客户端去重").

Last-will + online presence (P4 — XIU-103)
------------------------------------------
The aiomqtt session is configured with ``will=Will(edge/<id>/lwt, ...,
retain=True)`` so the broker publishes an ``{"state": "offline"}`` frame
on any ungraceful disconnect (TCP RST, edge power-cycle, keepalive
timeout). Whenever a session opens successfully the transport publishes
an ``{"state": "online"}`` retained frame on the same topic — so the
last retained payload on the broker always reflects the edge's current
presence. The center's :mod:`fleet.mqtt_transport` subscriber catches
both forms and updates :class:`fleet.models.EdgeNode.status` accordingly.
This replaces the WS heartbeat path (M1/M2 :class:`FleetConsumer`).

Connection lifecycle
--------------------
The center can bounce or the broker can restart at any time. aiomqtt
raises :class:`aiomqtt.MqttError` from any pending operation on the
underlying client when that happens; this class catches it inside
:meth:`_ensure_client`, transparently reconnects with exponential
backoff, and lets the caller retry the publish. Until the broker is
reachable :meth:`publish` keeps raising :class:`MqttError` so the
uplink loop can park itself instead of hot-spinning.

Outbox prune semantics
----------------------
:meth:`aiomqtt.Client.publish` at QoS 1 returns *after* the broker
PUBACK, so a successful return is end-to-end delivery confirmation
between the edge and the broker. The transport exposes
``confirms_on_publish = True``; the uplink loop prunes the
just-published outbox row on its own. During a broker outage publishes
block / raise :class:`MqttError`, the rows stay in the durable outbox,
and aiomqtt's reconnect drains them at QoS 1 once the broker is back —
the seq counter is monotonic and the broker dedup'es by packet id so
the chaos test sees no gap in the recovered stream (P4 verification).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

from .base import Transport

logger = logging.getLogger(__name__)


# Match Phase 2 P1's subscriber QoS — both sides at 1 means the broker
# handles the PUBACK / dedup flow end-to-end.
_PUBLISH_QOS = 1

# Reconnect backoff for transient broker outages. Mirrors the cadence
# used by the center subscriber (1 s, 2 s, ... cap 30 s) so a network
# blip cleans itself up in one or two iterations.
_RECONNECT_BACKOFF_INITIAL = 1.0
_RECONNECT_BACKOFF_MAX = 30.0

# Phase 2 P4 (XIU-103) — last-will + online presence on edge/<id>/lwt.
# A retained payload on the LWT topic means the *latest* value on the
# broker is always the edge's current presence state: an "offline" left
# behind by the broker on ungraceful disconnect, or an "online" pushed
# by the edge on (re)connect. Both directions go at QoS 1 retained=True.
LWT_STATE_ONLINE = "online"
LWT_STATE_OFFLINE = "offline"


def lwt_topic(edge_id: str) -> str:
    """Stable accessor for the LWT topic — used by tests + the will builder."""
    return f"edge/{edge_id}/lwt"


def _lwt_payload(edge_id: str, state: str) -> bytes:
    """Build the JSON payload the broker stamps on edge/<id>/lwt.

    The schema is intentionally tiny so the center subscriber can fold
    it into ``EdgeNode.status`` without a separate parsing layer:

    * ``state`` — ``"online"`` (live retained from the edge) or
      ``"offline"`` (broker-published on disconnect via the WILL).
    * ``edge_id`` — convenience echo so a downstream inspector tooling
      doesn't have to re-parse the topic.
    * ``ts`` — wall-clock at the moment the payload was constructed,
      ISO-8601. For ``offline`` this is the moment the connecting client
      built the WILL (i.e. session start); the actual disconnect time is
      recorded by the center handler.
    """
    body = {
        "state": state,
        "edge_id": edge_id,
        "ts": datetime.now(tz=timezone.utc).isoformat(),
    }
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


# Allow dependency injection from tests. Production code leaves it ``None``
# and we import ``aiomqtt`` lazily so the rest of the agent can still load
# on a host without aiomqtt installed.
ClientFactory = Callable[[], Any]


def _parse_broker_url(url: str) -> Dict[str, Any]:
    """Normalise ``mqtt://host:port`` / ``mqtts://...`` into kwargs.

    The XIU-102 default is ``mqtt://mosquitto:1883``; ``mqtts://`` flips
    TLS on (mosquitto 2.x serves TLS on whatever port the operator
    configures). Bare ``host:port`` (without scheme) is accepted as a
    convenience — aiomqtt also tolerates that shape.
    """
    if "://" not in url:
        url = "mqtt://" + url
    parsed = urlparse(url)
    scheme = (parsed.scheme or "mqtt").lower()
    if scheme not in ("mqtt", "mqtts"):
        raise ValueError(f"unsupported MQTT broker scheme: {scheme!r}")
    host = parsed.hostname or "mosquitto"
    default_port = 8883 if scheme == "mqtts" else 1883
    port = parsed.port or default_port
    return {
        "hostname": host,
        "port": port,
        "tls": scheme == "mqtts",
        "username": parsed.username or None,
        "password": parsed.password or None,
    }


def topic_for(edge_id: str, frame_type: str) -> str:
    """Centralise the topic naming so tests stay in sync with the impl."""
    return f"edge/{edge_id}/uplink/{frame_type}"


class MqttTransport(Transport):
    """aiomqtt-based uplink publisher (QoS 1, prune-on-broker-ack)."""

    # QoS 1 PUBACK is the delivery signal — see module docstring.
    confirms_on_publish = True

    def __init__(
        self,
        broker_url: str,
        edge_id: str,
        *,
        client_id: Optional[str] = None,
        client_factory: Optional[ClientFactory] = None,
        keepalive: int = 60,
    ) -> None:
        self._broker_url = broker_url
        self._edge_id = edge_id
        self._client_id = client_id or f"edge-{edge_id}"
        self._keepalive = int(keepalive)
        self._client_factory = client_factory
        # The active aiomqtt session. ``None`` until :meth:`connect` (or the
        # first lazy publish) has brought it up; reset to ``None`` on every
        # MqttError so the next publish reconnects.
        self._client: Any = None
        self._lock = asyncio.Lock()

    # ---- lifecycle --------------------------------------------------------

    async def connect(self) -> None:
        """Bring the broker session up, retrying on transient failures.

        The agent typically calls this once at startup, then publishes
        through the same session. A later :class:`aiomqtt.MqttError` from
        ``publish`` clears ``self._client`` so the *next* call re-runs
        the same connect path lazily. That keeps the reconnect logic in
        one place rather than scattered across every send_* method.
        """
        async with self._lock:
            if self._client is not None:
                return
            await self._open_session()

    async def close(self) -> None:
        """Tear the broker session down cleanly.

        Publishes a final ``{state: offline}`` retained payload on
        ``edge/<id>/lwt`` *before* disconnecting so the broker's last
        retained value reflects a graceful shutdown (P4 — XIU-103).
        The session WILL is for *ungraceful* disconnects; on a planned
        stop we want the broker to see the right state immediately.
        """
        async with self._lock:
            if self._client is None:
                return
            client = self._client
            self._client = None
            errors = self._mqtt_error_type()
            try:
                await client.publish(
                    lwt_topic(self._edge_id),
                    payload=_lwt_payload(self._edge_id, LWT_STATE_OFFLINE),
                    qos=_PUBLISH_QOS,
                    retain=True,
                )
            except errors as exc:
                # Best-effort: the broker may already be gone, in which
                # case the WILL will fire when the TCP socket reaps.
                logger.info(
                    "mqtt_transport: offline LWT publish on close failed "
                    "(%s) — relying on broker WILL", exc,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "mqtt_transport: offline LWT publish raised unexpectedly"
                )
            try:
                await client.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "mqtt_transport: error closing aiomqtt session — ignored"
                )

    # ---- internals --------------------------------------------------------

    def _build_client(self):
        """Construct an aiomqtt.Client from the broker URL / overrides.

        Every production session carries the LWT (XIU-103 P4): the
        broker auto-publishes ``edge/<id>/lwt`` ``{state: offline}``
        retained on any ungraceful disconnect (TCP RST, edge crash,
        keepalive timeout). The matching ``online`` retained payload is
        published by :meth:`_open_session` immediately after the session
        comes up, so the broker's last-retained value always reflects
        the edge's true presence.
        """
        if self._client_factory is not None:
            return self._client_factory()
        # Local import so a host without aiomqtt installed can still
        # import the rest of the edge-agent (e.g. for WS-only runs).
        import aiomqtt  # type: ignore

        kw = _parse_broker_url(self._broker_url)
        client_kwargs = {
            "hostname": kw["hostname"],
            "port": kw["port"],
            "identifier": self._client_id,
            "keepalive": self._keepalive,
            "will": aiomqtt.Will(
                topic=lwt_topic(self._edge_id),
                payload=_lwt_payload(self._edge_id, LWT_STATE_OFFLINE),
                qos=_PUBLISH_QOS,
                retain=True,
            ),
        }
        if kw.get("username"):
            client_kwargs["username"] = kw["username"]
        if kw.get("password"):
            client_kwargs["password"] = kw["password"]
        if kw.get("tls"):
            # aiomqtt accepts a pre-built TLSParameters object; pass
            # ``True`` to use platform defaults. Operator-supplied certs
            # are a later milestone (P5+).
            import ssl

            client_kwargs["tls_context"] = ssl.create_default_context()
        return aiomqtt.Client(**client_kwargs)

    @staticmethod
    def _mqtt_error_type() -> tuple:
        """Resolve aiomqtt.MqttError lazily so import-time stays cheap."""
        try:
            import aiomqtt  # type: ignore

            return (aiomqtt.MqttError,)
        except ImportError:
            # If aiomqtt isn't installed the production factory will
            # blow up at connect time with a clear ImportError — there
            # is nothing left to retry on. Catch Exception so tests
            # injecting a custom client_factory keep working.
            return (Exception,)

    async def _open_session(self) -> None:
        """Open one aiomqtt session, with exponential reconnect.

        Internal — caller already holds ``self._lock``. Returns once
        ``self._client`` references an active session AND the
        ``edge/<id>/lwt`` ``{state: online}`` retained announcement has
        been published (P4 — XIU-103). The online publish is part of
        the connect path on purpose: a session that came up but cannot
        send a tiny LWT replacement payload is degraded enough that we
        prefer to tear it back down and let the next reconnect retry,
        rather than leave the broker holding a stale ``offline``
        retained payload while the edge is actually up.
        """
        backoff = _RECONNECT_BACKOFF_INITIAL
        errors = self._mqtt_error_type()
        while True:
            client = self._build_client()
            try:
                await client.__aenter__()
            except errors as exc:
                logger.warning(
                    "mqtt_transport: connect to %s failed (%s) — retry in %.1fs",
                    self._broker_url, exc, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, _RECONNECT_BACKOFF_MAX)
                continue
            try:
                await client.publish(
                    lwt_topic(self._edge_id),
                    payload=_lwt_payload(self._edge_id, LWT_STATE_ONLINE),
                    qos=_PUBLISH_QOS,
                    retain=True,
                )
            except errors as exc:
                logger.warning(
                    "mqtt_transport: online LWT publish failed (%s) — "
                    "dropping session and retrying", exc,
                )
                try:
                    await client.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, _RECONNECT_BACKOFF_MAX)
                continue
            self._client = client
            logger.info(
                "mqtt_transport: connected to %s as %s (lwt=online)",
                self._broker_url, self._client_id,
            )
            return

    async def _publish_frame(self, frame: Dict[str, Any], frame_type: str) -> None:
        """Publish one frame at QoS 1; on MqttError drop the session.

        The caller (the uplink loop) is expected to retry — by the time
        it does, the next publish will re-open the session via the same
        ``_open_session`` path.
        """
        # Lazy connect lets tests publish without an explicit ``connect``.
        if self._client is None:
            await self.connect()
        topic = topic_for(self._edge_id, frame_type)
        payload = json.dumps(frame, ensure_ascii=False).encode("utf-8")
        errors = self._mqtt_error_type()
        try:
            await self._client.publish(topic, payload=payload, qos=_PUBLISH_QOS)
        except errors as exc:
            logger.warning(
                "mqtt_transport: publish %s failed (%s) — session dropped",
                topic, exc,
            )
            # Drop the dead session so the next publish reconnects.
            stale = self._client
            self._client = None
            try:
                await stale.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
            raise

    # ---- Transport API ----------------------------------------------------

    async def send_state(self, frame: Dict[str, Any]) -> None:
        await self._publish_frame(frame, "lifecycle")

    async def send_sample(self, frame: Dict[str, Any]) -> None:
        await self._publish_frame(frame, "sample_batch")

    async def send_alarm(self, frame: Dict[str, Any]) -> None:
        await self._publish_frame(frame, "alarm_event")
