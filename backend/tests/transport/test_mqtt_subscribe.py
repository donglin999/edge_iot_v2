"""MQTT center subscriber tests — XIU-100 Phase 2 P1.

The real subscriber is asyncio-driven against an `aiomqtt.Client`. We
inject a fake client through ``run_subscriber(..., client_factory=...)``
so the test never opens a TCP socket and stays deterministic across CI
without an external broker.

What we assert (matches the issue's verification list):

1. The subscriber issues both subscriptions at QoS 1.
2. Each received message is parsed and dispatched through the
   :class:`UplinkRouter` with the edge name extracted from the topic.
3. After a ``MqttError`` is raised the subscriber reconnects and
   re-subscribes — i.e. a session drop does not leave QoS 1 in limbo.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import List

import pytest

from fleet import mqtt_transport
from fleet.mqtt_transport import (
    LWT_TOPIC,
    MqttTransportConfig,
    UPLINK_TOPIC,
    parse_topic,
    run_subscriber,
)
from fleet.uplink_router import UplinkRouter


# A stand-in for aiomqtt.MqttError; the production subscriber catches the
# real one when the client factory is the default, and treats any
# Exception as reconnectable when no aiomqtt module is available, so this
# subclassing keeps the assertion semantics regardless of install state.
class _FakeMqttError(Exception):
    pass


@dataclass
class _FakeMessage:
    topic: str
    payload: bytes


@dataclass
class _FakeClient:
    """Async-context-manager + async-iterator stand-in for aiomqtt.Client.

    A test scripts a list of "sessions": each session is the messages
    that should be delivered before the loop raises a synthetic
    disconnect (or ``None`` to end the test cleanly via cancellation).
    """

    sessions: List[List[_FakeMessage]]
    subscribe_calls: List[tuple] = field(default_factory=list)
    enter_count: int = 0
    _session_idx: int = 0
    _disconnect_after_each: bool = True

    async def __aenter__(self):
        self.enter_count += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def subscribe(self, topic, qos=0):
        self.subscribe_calls.append((topic, qos))

    @property
    def messages(self):
        return self._iter()

    async def _iter(self):
        if self._session_idx >= len(self.sessions):
            # No more scripted sessions — block forever until cancelled.
            await asyncio.Event().wait()
            return
        msgs = self.sessions[self._session_idx]
        self._session_idx += 1
        for m in msgs:
            yield m
        if self._disconnect_after_each:
            raise _FakeMqttError("synthetic disconnect")


@pytest.fixture
def patch_backoff(monkeypatch):
    """Drop the reconnect backoff to ~0 so the test isn't slowed by sleeps."""
    monkeypatch.setattr(mqtt_transport, "_BACKOFF_INITIAL", 0.0)
    monkeypatch.setattr(mqtt_transport, "_BACKOFF_MAX", 0.0)


@pytest.mark.asyncio
async def test_subscribes_to_uplink_and_lwt_at_qos_1(patch_backoff):
    """First session: both topic wildcards subscribed at QoS 1."""
    client = _FakeClient(sessions=[[]])  # one empty session, then end

    config = MqttTransportConfig(host="ignored", port=0)
    task = asyncio.create_task(
        run_subscriber(
            config,
            UplinkRouter(),
            client_factory=lambda: client,
            reconnect_exceptions=(_FakeMqttError,),
        )
    )

    # Yield enough times for the subscriber to enter the session, call
    # subscribe twice, raise the synthetic disconnect, and loop back into
    # the second (blocking) session.
    for _ in range(20):
        await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert (UPLINK_TOPIC, 1) in client.subscribe_calls
    assert (LWT_TOPIC, 1) in client.subscribe_calls


@pytest.mark.asyncio
async def test_message_dispatched_to_router_with_edge_name(patch_backoff):
    """A topic payload reaches the router parsed + tagged with the edge id."""
    msg = _FakeMessage(
        topic="edge/edge-1/uplink/heartbeat",
        payload=json.dumps({"type": "heartbeat", "edge_id": "edge-1"}).encode(),
    )
    client = _FakeClient(sessions=[[msg]])
    router = UplinkRouter()
    dispatched: List[tuple] = []

    async def handler(edge, frame):
        dispatched.append((edge, frame))

    router.set_handler(handler)

    config = MqttTransportConfig(host="ignored", port=0)
    task = asyncio.create_task(
        run_subscriber(
            config,
            router,
            client_factory=lambda: client,
            reconnect_exceptions=(_FakeMqttError,),
        )
    )
    for _ in range(30):
        await asyncio.sleep(0)
        if dispatched:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert dispatched, "router never saw the message"
    edge, frame = dispatched[0]
    assert edge == "edge-1"
    assert frame == {"type": "heartbeat", "edge_id": "edge-1"}


@pytest.mark.asyncio
async def test_lwt_topic_dispatched_with_lwt_frame_type(patch_backoff):
    """An ``edge/<id>/lwt`` retained payload reaches the router as type=lwt."""
    msg = _FakeMessage(
        topic="edge/edge-2/lwt",
        payload=b'{"status": "offline"}',
    )
    client = _FakeClient(sessions=[[msg]])
    router = UplinkRouter()
    seen: List[tuple] = []

    async def handler(edge, frame):
        seen.append((edge, frame))
    router.set_handler(handler)

    config = MqttTransportConfig(host="ignored", port=0)
    task = asyncio.create_task(
        run_subscriber(
            config,
            router,
            client_factory=lambda: client,
            reconnect_exceptions=(_FakeMqttError,),
        )
    )
    for _ in range(30):
        await asyncio.sleep(0)
        if seen:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert seen, "lwt frame never reached the router"
    edge, frame = seen[0]
    assert edge == "edge-2"
    assert frame.get("type") == "lwt"
    assert frame.get("status") == "offline"


@pytest.mark.asyncio
async def test_reconnects_and_resubscribes_after_session_drop(patch_backoff):
    """A synthetic disconnect triggers reconnect and a fresh subscribe pair."""
    session_a = [
        _FakeMessage(
            topic="edge/edge-1/uplink/heartbeat",
            payload=b'{"type":"heartbeat"}',
        )
    ]
    session_b = [
        _FakeMessage(
            topic="edge/edge-1/uplink/heartbeat",
            payload=b'{"type":"heartbeat","n":2}',
        )
    ]
    client = _FakeClient(sessions=[session_a, session_b])

    config = MqttTransportConfig(host="ignored", port=0)
    task = asyncio.create_task(
        run_subscriber(
            config,
            UplinkRouter(),
            client_factory=lambda: client,
            reconnect_exceptions=(_FakeMqttError,),
        )
    )
    # Drive the event loop long enough for both sessions to play out:
    # session A → MqttError → backoff (≈0 s) → session B → MqttError →
    # backoff → block in session C (empty default).
    for _ in range(60):
        await asyncio.sleep(0)
        if client.enter_count >= 2 and len(client.subscribe_calls) >= 4:
            break

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The client was entered at least twice (initial + after reconnect),
    # and each session installed both subscriptions — so the subscribe
    # log has 2 * 2 = 4 entries minimum.
    assert client.enter_count >= 2
    uplink_calls = [c for c in client.subscribe_calls if c[0] == UPLINK_TOPIC]
    lwt_calls = [c for c in client.subscribe_calls if c[0] == LWT_TOPIC]
    assert len(uplink_calls) >= 2
    assert len(lwt_calls) >= 2
    # All at QoS 1.
    assert all(qos == 1 for _, qos in client.subscribe_calls)


@pytest.mark.asyncio
async def test_bad_json_is_dropped_not_propagated(patch_backoff):
    """A non-JSON payload must not crash the subscriber or reach the router."""
    msgs = [
        _FakeMessage(topic="edge/edge-1/uplink/heartbeat", payload=b"not-json"),
        _FakeMessage(
            topic="edge/edge-1/uplink/heartbeat",
            payload=b'{"type":"heartbeat","ok":true}',
        ),
    ]
    client = _FakeClient(sessions=[msgs])
    router = UplinkRouter()
    seen: List[tuple] = []

    async def handler(edge, frame):
        seen.append((edge, frame))
    router.set_handler(handler)

    config = MqttTransportConfig(host="ignored", port=0)
    task = asyncio.create_task(
        run_subscriber(
            config,
            router,
            client_factory=lambda: client,
            reconnect_exceptions=(_FakeMqttError,),
        )
    )
    for _ in range(40):
        await asyncio.sleep(0)
        if seen:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Only the second (valid) message made it through.
    assert seen == [("edge-1", {"type": "heartbeat", "ok": True})]


# ---------------------------------------------------------------------------
# parse_topic — pure, no asyncio needed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "topic,expected",
    [
        ("edge/edge-1/uplink/heartbeat", ("edge-1", "heartbeat")),
        ("edge/edge-1/uplink/sample_batch", ("edge-1", "sample_batch")),
        ("edge/edge-7/lwt", ("edge-7", "lwt")),
        # Empty edge id → reject.
        ("edge//uplink/heartbeat", (None, None)),
        # Missing leaf → reject.
        ("edge/edge-1/uplink/", (None, None)),
        # Wrong root → reject.
        ("$SYS/broker/uptime", (None, None)),
        # Wrong section → reject.
        ("edge/edge-1/downlink/apply_config", (None, None)),
    ],
)
def test_parse_topic(topic, expected):
    assert parse_topic(topic) == expected


# ---------------------------------------------------------------------------
# ASGI lifespan handler — the daphne entry point
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifespan_handler_skips_startup_when_disabled(monkeypatch):
    """``FLEET_MQTT_ENABLED=false`` ⇒ no subscriber task, clean handshake."""
    from django.conf import settings
    from fleet.mqtt_transport import default_manager, lifespan_handler

    monkeypatch.setattr(settings, "FLEET_MQTT_ENABLED", False, raising=False)

    received: List[dict] = []

    inbox = asyncio.Queue()
    await inbox.put({"type": "lifespan.startup"})
    await inbox.put({"type": "lifespan.shutdown"})

    async def receive():
        return await inbox.get()

    async def send(msg):
        received.append(msg)

    await lifespan_handler(None, receive, send)

    assert [m["type"] for m in received] == [
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    ]
    assert not default_manager.running


@pytest.mark.asyncio
async def test_lifespan_handler_starts_and_stops_subscriber_when_enabled(
    monkeypatch, patch_backoff,
):
    """Startup spawns the run_subscriber task, shutdown cancels it cleanly."""
    from django.conf import settings
    from fleet import mqtt_transport
    from fleet.mqtt_transport import default_manager, lifespan_handler

    # Force the manager to use our fake client + a known-finite reconnect
    # exception so the test never depends on the real aiomqtt module.
    fake_client = _FakeClient(sessions=[[]])

    real_run_subscriber = mqtt_transport.run_subscriber

    async def wrapped_run_subscriber(config, router=None, *, client_factory=None,
                                      reconnect_exceptions=None):
        return await real_run_subscriber(
            config,
            router or mqtt_transport.default_router,
            client_factory=lambda: fake_client,
            reconnect_exceptions=(_FakeMqttError,),
        )

    monkeypatch.setattr(mqtt_transport, "run_subscriber", wrapped_run_subscriber)
    monkeypatch.setattr(settings, "FLEET_MQTT_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "FLEET_MQTT_HOST", "ignored", raising=False)
    monkeypatch.setattr(settings, "FLEET_MQTT_PORT", 0, raising=False)

    received: List[dict] = []
    inbox = asyncio.Queue()
    await inbox.put({"type": "lifespan.startup"})

    async def receive():
        return await inbox.get()

    async def send(msg):
        received.append(msg)
        if msg["type"] == "lifespan.startup.complete":
            # After startup completes, let the subscriber tick the loop
            # a few times so it actually enters its session, then queue
            # shutdown so the handler unwinds.
            for _ in range(10):
                await asyncio.sleep(0)
            await inbox.put({"type": "lifespan.shutdown"})

    try:
        await lifespan_handler(None, receive, send)
        assert [m["type"] for m in received] == [
            "lifespan.startup.complete",
            "lifespan.shutdown.complete",
        ]
        # After shutdown the manager owns no task.
        assert not default_manager.running
    finally:
        # Defensive: if any prior test left the singleton dirty, scrub it.
        await default_manager.stop()


# ---------------------------------------------------------------------------
# Optional integration smoke against a real broker. Skipped unless the
# operator opts in via FLEET_MQTT_TEST_BROKER=host[:port]. The Phase-2 P1
# acceptance criterion (``docker logs center-django`` showing the connect
# line) is covered by this when run against the live ``docker compose -f
# docker-compose.center.yml up`` stack.
# ---------------------------------------------------------------------------


import os
import socket


def _live_broker():
    spec = os.environ.get("FLEET_MQTT_TEST_BROKER")
    if not spec:
        return None
    host, _, port_s = spec.partition(":")
    port = int(port_s or 1883)
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return host, port
    except OSError:
        return None


@pytest.mark.asyncio
async def test_live_broker_subscribe_and_publish_roundtrip():
    """End-to-end: subscribe via the transport, publish via aiomqtt, receive."""
    broker = _live_broker()
    if broker is None:
        pytest.skip(
            "FLEET_MQTT_TEST_BROKER not set or broker unreachable — "
            "run `docker compose -f docker-compose.center.yml up -d mosquitto` "
            "and re-run with FLEET_MQTT_TEST_BROKER=127.0.0.1:1883"
        )

    aiomqtt = pytest.importorskip("aiomqtt")
    host, port = broker

    router = UplinkRouter()
    seen: asyncio.Queue = asyncio.Queue()

    async def handler(edge, frame):
        await seen.put((edge, frame))

    router.set_handler(handler)

    config = MqttTransportConfig(host=host, port=port, client_id=f"test-sub-{os.getpid()}")
    task = asyncio.create_task(run_subscriber(config, router))
    try:
        # Wait for the subscriber to be subscribed (heuristic — give the
        # CONNECT/SUBSCRIBE roundtrip a generous budget for slow CI).
        await asyncio.sleep(0.5)

        async with aiomqtt.Client(hostname=host, port=port,
                                  identifier=f"test-pub-{os.getpid()}") as pub:
            payload = json.dumps({"type": "heartbeat", "edge_id": "live-1"}).encode()
            await pub.publish(
                "edge/live-1/uplink/heartbeat", payload=payload, qos=1,
            )
        edge, frame = await asyncio.wait_for(seen.get(), timeout=3.0)
        assert edge == "live-1"
        assert frame["type"] == "heartbeat"
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
