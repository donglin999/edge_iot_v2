"""aiomqtt-backed uplink transport (Phase 2 P3 — XIU-102).

Publishes the seq'd uplink frames (lifecycle / sample_batch / alarm_event)
to the center MQTT broker stood up by Phase 2 P1 (XIU-100). Topic layout
mirrors the center subscriber's expectations
(:mod:`backend.fleet.mqtt_transport`):

* ``edge/<edge_id>/uplink/lifecycle``
* ``edge/<edge_id>/uplink/sample_batch``
* ``edge/<edge_id>/uplink/alarm_event``

All publishes go at QoS 1. The center subscriber wildcards on
``edge/+/uplink/#`` at the same QoS so the per-message PUBACK / PUBREL
flow is in place end-to-end. Combined with the existing client-side
``monotonic_seq`` dedup on the center, that gives us the
"at-least-once + dedup = exactly-once" semantics called out in the issue
("QoS 1 + seq 客户端去重").

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
``confirms_on_publish = True``; the uplink loop will then prune the
just-publised outbox row on its own. The center-side ack-over-WS path is
unused in MQTT mode (P4 will revisit this when last-will + outbox
reconnect over MQTT land).
"""
from __future__ import annotations

import asyncio
import json
import logging
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
        async with self._lock:
            if self._client is None:
                return
            client = self._client
            self._client = None
            try:
                await client.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "mqtt_transport: error closing aiomqtt session — ignored"
                )

    # ---- internals --------------------------------------------------------

    def _build_client(self):
        """Construct an aiomqtt.Client from the broker URL / overrides."""
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
        ``self._client`` references an active session.
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
            self._client = client
            logger.info(
                "mqtt_transport: connected to %s as %s",
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
