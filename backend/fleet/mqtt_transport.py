"""Center MQTT transport — Phase 2 (XIU-100 P1 + XIU-101 P2).

This module owns both directions of the center ↔ broker plumbing:

* **Uplink subscriber** (P1, XIU-100): a long-lived asyncio task driven by
  the daphne ASGI lifespan that connects to the broker, subscribes to the
  edge uplink topic tree at QoS 1, parses each frame as JSON and forwards
  it to :class:`fleet.uplink_router.UplinkRouter`.
* **Downlink publisher** (P2, XIU-101): a process-wide paho-mqtt client
  that publishes center → edge commands on ``edge/<edge_id>/cmd/<type>``.
  ``publish_command()`` is the sync entry point — it lazy-connects the
  publisher on first call so the existing sync DRF/signal call sites can
  use it without an asyncio bridge.

Topic tree (plan §9.3 / §9.4):

* ``edge/<edge_id>/uplink/<frame_type>``  — edge → center uplink frames
* ``edge/<edge_id>/lwt``                  — broker-published last-will
* ``edge/<edge_id>/cmd/<cmd_type>``       — center → edge commands

Subscriber lifecycle:

* connect → subscribe to both wildcards → enter the per-message async loop
* on ``aiomqtt.MqttError`` (network drop, broker restart) → exponential
  backoff and reconnect, re-subscribing each time so QoS 1 message flow
  resumes without operator intervention
* on ``asyncio.CancelledError`` (lifespan shutdown) → close the underlying
  client and return cleanly

The downlink direction is gated by the ``FLEET_TRANSPORT`` setting
(``mqtt|ws|both``, default ``mqtt``) at the dispatcher in ``services.py``;
the publisher itself is mode-agnostic.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from .uplink_router import UplinkRouter, default_router

logger = logging.getLogger(__name__)


# Subscriptions installed on every (re)connect. The order doesn't matter,
# but the broker only acks subscribe RTTs one at a time so we batch by
# issuing them sequentially in the async context.
UPLINK_TOPIC = "edge/+/uplink/#"
LWT_TOPIC = "edge/+/lwt"

# QoS 1 — at-least-once. Center handlers must be idempotent (the existing
# WS path already enforces this via ``monotonic_seq`` dedup, so an MQTT
# redelivery folds into the same code).
SUBSCRIPTION_QOS = 1

# Reconnect backoff: 1 s, 2 s, 4 s, ... capped at 30 s. Pure transient
# loss (broker bounce) recovers in the first one or two iterations; the
# cap prevents long network outages from making a recovery storm.
_BACKOFF_INITIAL = 1.0
_BACKOFF_MAX = 30.0


@dataclass
class MqttTransportConfig:
    host: str
    port: int = 1883
    client_id: str = "center-fleet-subscriber"
    username: Optional[str] = None
    password: Optional[str] = None
    # Keepalive in seconds. mosquitto's default is 60; we leave the same
    # so a half-open TCP keeps tripping the broker-side disconnect first.
    keepalive: int = 60


def parse_topic(topic: str) -> Tuple[Optional[str], Optional[str]]:
    """Pull ``(edge_name, frame_type)`` out of an MQTT topic.

    Returns ``(edge, frame_type)`` for an ``edge/<edge>/uplink/<type>``
    or ``edge/<edge>/lwt`` topic, ``(None, None)`` otherwise. The
    leading ``edge/`` prefix is required so a stray subscription (e.g.
    ``$SYS``) cannot smuggle a malformed topic into the router.
    """
    if not topic.startswith("edge/"):
        return None, None
    parts = topic.split("/")
    # ['edge', edge_id, 'uplink', frame_type, ...]  or
    # ['edge', edge_id, 'lwt']
    if len(parts) < 3 or not parts[1]:
        return None, None
    edge = parts[1]
    section = parts[2]
    if section == "lwt":
        return edge, "lwt"
    if section == "uplink" and len(parts) >= 4 and parts[3]:
        return edge, parts[3]
    return None, None


async def _handle_message(message, router: UplinkRouter) -> None:
    """Parse one aiomqtt message and dispatch it through ``router``.

    Bad JSON or malformed topics are logged and dropped — we never let
    one poisoned frame tear down the subscriber loop.
    """
    topic = str(message.topic)
    edge, frame_type = parse_topic(topic)
    if edge is None:
        logger.warning("mqtt_transport: ignoring unparsable topic=%r", topic)
        return
    payload = message.payload
    if isinstance(payload, (bytes, bytearray)):
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning(
                "mqtt_transport: non-utf8 payload edge=%s topic=%s — dropped",
                edge, topic,
            )
            return
    else:
        text = str(payload)
    try:
        frame = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning(
            "mqtt_transport: bad JSON edge=%s topic=%s err=%s — dropped",
            edge, topic, exc,
        )
        return
    if not isinstance(frame, dict):
        logger.warning(
            "mqtt_transport: frame must be an object edge=%s topic=%s — dropped",
            edge, topic,
        )
        return
    # LWT topic carries no ``type`` field by convention; stamp one so the
    # router can route on it.
    if frame_type == "lwt":
        frame.setdefault("type", "lwt")
    await router.dispatch(edge, frame)


async def _run_session(client, router: UplinkRouter) -> None:
    """One connected MQTT session: subscribe + consume until error."""
    await client.subscribe(UPLINK_TOPIC, qos=SUBSCRIPTION_QOS)
    await client.subscribe(LWT_TOPIC, qos=SUBSCRIPTION_QOS)
    logger.info(
        "mqtt_transport: subscribed to %s + %s (qos %d)",
        UPLINK_TOPIC, LWT_TOPIC, SUBSCRIPTION_QOS,
    )
    async for message in client.messages:
        try:
            await _handle_message(message, router)
        except Exception:  # noqa: BLE001
            logger.exception("mqtt_transport: handler crashed — continuing")


async def run_subscriber(
    config: MqttTransportConfig,
    router: UplinkRouter = default_router,
    *,
    client_factory=None,
    reconnect_exceptions: Optional[tuple] = None,
) -> None:
    """Long-running task: connect, subscribe, consume, reconnect forever.

    ``client_factory`` is an injection seam for tests — it must return an
    object satisfying the ``aiomqtt.Client`` async-context-manager / async
    iterator protocol. Production code leaves it ``None`` and we import
    ``aiomqtt`` lazily so the rest of the backend keeps importing even on
    hosts that have not yet installed the dependency.

    ``reconnect_exceptions`` overrides which exception class(es) trigger a
    backoff-and-reconnect rather than tearing down the task. Defaults to
    ``aiomqtt.MqttError`` when ``aiomqtt`` is importable, else
    :class:`Exception`. Tests use this seam to raise their own marker
    type without taking a transitive dependency on aiomqtt.
    """
    if client_factory is None:
        import aiomqtt  # local import so settings.py never fails on it

        def client_factory():  # type: ignore[no-redef]
            return aiomqtt.Client(
                hostname=config.host,
                port=config.port,
                identifier=config.client_id,
                username=config.username,
                password=config.password,
                keepalive=config.keepalive,
            )

    if reconnect_exceptions is None:
        try:
            import aiomqtt  # noqa: F401  — imported above when default factory
            reconnectable: tuple = (aiomqtt.MqttError,)  # type: ignore[attr-defined]
        except ImportError:
            reconnectable = (Exception,)
    else:
        reconnectable = reconnect_exceptions

    backoff = _BACKOFF_INITIAL
    while True:
        try:
            async with client_factory() as client:
                logger.info(
                    "mqtt_transport: connected to %s:%d (client_id=%s)",
                    config.host, config.port, config.client_id,
                )
                # Reset the backoff once a connection succeeds — the next
                # drop should retry quickly, not at the prior max.
                backoff = _BACKOFF_INITIAL
                await _run_session(client, router)
        except asyncio.CancelledError:
            logger.info("mqtt_transport: cancelled — shutting down subscriber")
            raise
        except reconnectable as exc:  # type: ignore[misc]
            logger.warning(
                "mqtt_transport: session ended (%s) — reconnecting in %.1fs",
                exc, backoff,
            )
        except Exception:  # noqa: BLE001
            # An unexpected error must not kill the whole transport;
            # log and retry like a normal disconnect.
            logger.exception(
                "mqtt_transport: unexpected error in session — reconnecting in %.1fs",
                backoff,
            )
        try:
            await asyncio.sleep(backoff)
        except asyncio.CancelledError:
            logger.info("mqtt_transport: cancelled during backoff")
            raise
        backoff = min(backoff * 2.0, _BACKOFF_MAX)


# ---------------------------------------------------------------------------
# Process-wide subscriber manager + ASGI lifespan integration
# ---------------------------------------------------------------------------
#
# daphne drives the subscriber via the ASGI ``lifespan`` protocol — startup
# schedules the ``run_subscriber`` task, shutdown cancels it. The manager
# is a singleton: a second ``start()`` call returns the same task so a
# duplicated lifespan event (autoreload, test re-runs in-process) does not
# double-subscribe.


class _SubscriberManager:
    """Bridges the daphne lifecycle to one ``run_subscriber`` task."""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _config_from_settings(self) -> MqttTransportConfig:
        from django.conf import settings  # local: tolerate missing app config

        return MqttTransportConfig(
            host=str(getattr(settings, "FLEET_MQTT_HOST", "center-mosquitto")),
            port=int(getattr(settings, "FLEET_MQTT_PORT", 1883)),
            client_id=str(
                getattr(settings, "FLEET_MQTT_CLIENT_ID", "")
                or "center-fleet-subscriber"
            ),
            username=(str(getattr(settings, "FLEET_MQTT_USERNAME", "")) or None),
            password=(str(getattr(settings, "FLEET_MQTT_PASSWORD", "")) or None),
        )

    async def start(self) -> None:
        if self.running:
            return
        config = self._config_from_settings()
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(
            run_subscriber(config), name="fleet-mqtt-subscriber"
        )
        logger.info(
            "mqtt_transport: subscriber task scheduled (broker=%s:%d)",
            config.host, config.port,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        task, self._task = self._task, None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("mqtt_transport: subscriber raised on shutdown")
        logger.info("mqtt_transport: subscriber stopped")


# Singleton owned by the ASGI process. Tests construct their own manager.
default_manager = _SubscriberManager()


async def _on_startup() -> None:
    from django.conf import settings

    if not getattr(settings, "FLEET_MQTT_ENABLED", False):
        logger.info(
            "mqtt_transport: FLEET_MQTT_ENABLED=false — subscriber NOT started"
        )
        return
    await default_manager.start()


async def _on_shutdown() -> None:
    await default_manager.stop()
    # Tear the publisher down on the same lifespan shutdown so the paho
    # network thread does not outlive daphne. Wrapped in a thread-pool call
    # because ``loop_stop()`` joins the paho thread synchronously.
    await asyncio.get_running_loop().run_in_executor(None, default_publisher.stop)


async def lifespan_handler(scope, receive, send) -> None:
    """ASGI ``lifespan`` handler driving the MQTT subscriber start/stop.

    Channels' :class:`ProtocolTypeRouter` raises on unknown scope types,
    so the root ASGI app in :mod:`control_plane.asgi` intercepts
    ``lifespan`` and forwards to this handler ahead of the router.

    A failure in startup/shutdown is reported via the appropriate
    ``.failed`` event with the exception message; we do NOT re-raise — a
    broken MQTT subscriber must not block daphne from accepting HTTP and
    WebSocket traffic on the existing WS path during the grey-rollout.
    """
    while True:
        message = await receive()
        kind = message.get("type")
        if kind == "lifespan.startup":
            try:
                await _on_startup()
            except Exception as exc:  # noqa: BLE001
                logger.exception("mqtt_transport: startup hook failed")
                await send(
                    {"type": "lifespan.startup.failed", "message": str(exc)}
                )
                return
            await send({"type": "lifespan.startup.complete"})
        elif kind == "lifespan.shutdown":
            try:
                await _on_shutdown()
            except Exception as exc:  # noqa: BLE001
                logger.exception("mqtt_transport: shutdown hook failed")
                await send(
                    {"type": "lifespan.shutdown.failed", "message": str(exc)}
                )
                return
            await send({"type": "lifespan.shutdown.complete"})
            return
        else:
            logger.debug("mqtt_transport: ignoring lifespan event %r", kind)


# ---------------------------------------------------------------------------
# Downlink publisher — XIU-101 Phase 2 P2
# ---------------------------------------------------------------------------
#
# The sync callers (``fleet.services.dispatch_apply_config`` invoked from a
# DRF view / a ``transaction.on_commit`` signal handler) need a plain blocking
# entry point to publish ``apply_config`` (and, when added, ``restart_task``
# etc.) over MQTT. paho-mqtt's threaded client is the most natural fit — it
# owns its own network thread once ``loop_start()`` is called, and
# ``Client.publish()`` is thread-safe and returns immediately. Lazy-connecting
# on first publish keeps process startup decoupled from broker reachability,
# matching the soft-fail policy the subscriber side already follows.


# topic format for every center → edge command publish — kept module-level so
# tests can assert against it without hard-coding the f-string.
CMD_TOPIC_TEMPLATE = "edge/{edge_id}/cmd/{cmd_type}"


class _PublisherManager:
    """Process-wide paho-mqtt client used for center → edge command publish.

    Lazy: the first ``publish()`` call establishes the connection and starts
    paho's background network thread; subsequent calls reuse it. ``stop()``
    is invoked from the ASGI lifespan shutdown so the network thread does
    not outlive daphne. The class deliberately swallows connect / publish
    errors and returns ``False`` instead of raising — a broken broker must
    not propagate out of a config sync and 500 the DRF view; the WS path
    (when ``FLEET_TRANSPORT=both``) or the edge's reconnect re-fetch keeps
    convergence going.
    """

    def __init__(self) -> None:
        self._client = None  # type: Optional[Any]
        self._connected: bool = False
        self._lock = threading.Lock()

    def _config_from_settings(self) -> dict:
        from django.conf import settings  # local to tolerate missing app config

        return {
            "host": str(getattr(settings, "FLEET_MQTT_HOST", "center-mosquitto")),
            "port": int(getattr(settings, "FLEET_MQTT_PORT", 1883)),
            "client_id": str(
                getattr(settings, "FLEET_MQTT_PUBLISHER_CLIENT_ID", "")
                or "center-fleet-publisher"
            ),
            "username": (str(getattr(settings, "FLEET_MQTT_USERNAME", "")) or None),
            "password": (str(getattr(settings, "FLEET_MQTT_PASSWORD", "")) or None),
        }

    def _ensure_connected(self) -> bool:
        if self._connected and self._client is not None:
            return True
        with self._lock:
            if self._connected and self._client is not None:
                return True
            try:
                import paho.mqtt.client as mqtt
            except ImportError:
                logger.error(
                    "mqtt_publisher: paho-mqtt not installed — downlink disabled"
                )
                return False
            cfg = self._config_from_settings()
            try:
                client = mqtt.Client(client_id=cfg["client_id"], clean_session=True)
                if cfg["username"]:
                    client.username_pw_set(cfg["username"], cfg["password"] or "")
                client.connect(cfg["host"], cfg["port"], keepalive=60)
                client.loop_start()
            except Exception:  # noqa: BLE001
                logger.exception(
                    "mqtt_publisher: connect failed broker=%s:%d", cfg["host"], cfg["port"]
                )
                return False
            self._client = client
            self._connected = True
            logger.info(
                "mqtt_publisher: connected broker=%s:%d client_id=%s",
                cfg["host"], cfg["port"], cfg["client_id"],
            )
            return True

    def publish(self, topic: str, payload: bytes, qos: int = 1) -> bool:
        """Publish one message. Returns ``True`` iff paho accepted it."""
        if not self._ensure_connected():
            return False
        try:
            info = self._client.publish(topic, payload=payload, qos=qos)
        except Exception:  # noqa: BLE001
            logger.exception("mqtt_publisher: publish raised topic=%s", topic)
            return False
        # paho returns an MQTTMessageInfo whose ``rc`` is MQTT_ERR_SUCCESS (0)
        # on accept. We do NOT wait for PUBACK — the network thread retries
        # at QoS 1 and the edge dedupes ``apply_config`` by ``version``.
        rc = getattr(info, "rc", 0)
        if rc != 0:
            logger.warning(
                "mqtt_publisher: paho rejected publish topic=%s rc=%s", topic, rc
            )
            return False
        return True

    def stop(self) -> None:
        with self._lock:
            client, self._client = self._client, None
            self._connected = False
        if client is None:
            return
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:  # noqa: BLE001
            logger.exception("mqtt_publisher: shutdown raised")
        logger.info("mqtt_publisher: stopped")


# Singleton owned by the ASGI process; tests inject a fake via the
# ``publisher=`` kwarg on :func:`publish_command` or by monkey-patching this.
default_publisher = _PublisherManager()


def publish_command(
    edge_id: str,
    cmd_type: str,
    payload: Any,
    qos: int = 1,
    *,
    publisher: Optional[_PublisherManager] = None,
) -> bool:
    """Publish one center → edge command frame over MQTT.

    Topic format: ``edge/{edge_id}/cmd/{cmd_type}``. Payload is JSON-encoded
    via :func:`json.dumps` (utf-8 bytes on the wire). Returns ``True`` iff
    paho accepted the publish — best-effort at QoS 1, the broker handles
    retransmission to the edge.

    ``publisher`` is an injection seam for tests.
    """
    pub = publisher if publisher is not None else default_publisher
    topic = CMD_TOPIC_TEMPLATE.format(edge_id=edge_id, cmd_type=cmd_type)
    try:
        data = json.dumps(payload).encode("utf-8")
    except (TypeError, ValueError):
        logger.exception(
            "mqtt_publisher: payload not JSON-serialisable edge=%s cmd=%s",
            edge_id, cmd_type,
        )
        return False
    ok = pub.publish(topic, data, qos=qos)
    if ok:
        logger.info(
            "mqtt_publisher: published topic=%s qos=%d bytes=%d",
            topic, qos, len(data),
        )
    return ok
