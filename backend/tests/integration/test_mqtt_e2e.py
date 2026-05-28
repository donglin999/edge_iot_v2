"""End-to-end MQTT acceptance tests — Phase 2 P5 (XIU-104).

Wires the edge-side :class:`edge_agent.transport.mqtt_client.MqttTransport`
to the center-side :func:`fleet.mqtt_transport.run_subscriber` through an
in-process broker double so the full publisher → broker → subscriber →
router chain is exercised in one event loop. No real broker or docker
socket is required — that is :mod:`scripts.chaos_mqtt_broker_kill` 's job
(it has a ``--live`` mode for the docker-compose acceptance smoke).

What the issue (XIU-104 §1.2) asks for, with one test per scenario:

* **dual edge topic isolation** — two edges on the same broker publish to
  ``edge/a/...`` and ``edge/b/...`` independently; the center router sees
  each frame tagged with its publisher's ``edge_id`` and no cross-bleed.
* **QoS 1 restart resume** — an edge publishes some frames, ``close()``s
  the session, reopens, and keeps publishing. The broker receives every
  frame in strictly-increasing ``monotonic_seq`` order with no gap and
  no duplicate (the persistent counter in :mod:`edge_agent.outbox` is
  what makes that hold across restarts).
* **LWT → status flip** — an edge's ``edge/<id>/lwt`` retained payload
  reaches the center's :mod:`fleet.presence` handler which folds it into
  :class:`fleet.models.EdgeNode.status`. Online flips to ONLINE; an
  ungraceful disconnect (broker WILL fires) flips to OFFLINE + records
  one ``session.offline`` :class:`EdgeLifecycleEvent` row.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest


# The edge-agent package isn't installed into the backend's site-packages —
# this mirrors how scripts/ and other integration smokes pull it in.
_EDGE_SRC = Path(__file__).resolve().parents[3] / "edge-agent" / "src"
if str(_EDGE_SRC) not in sys.path:
    sys.path.insert(0, str(_EDGE_SRC))

from edge_agent.outbox import DurableOutbox  # noqa: E402
from edge_agent.transport.mqtt_client import (  # noqa: E402
    MqttTransport,
    lwt_topic,
    topic_for,
)

from fleet import mqtt_transport, presence  # noqa: E402
from fleet.mqtt_transport import (  # noqa: E402
    LWT_TOPIC,
    MqttTransportConfig,
    UPLINK_TOPIC,
    run_subscriber,
)
from fleet.uplink_router import UplinkRouter  # noqa: E402


# ---------------------------------------------------------------------------
# Shared MqttError stand-in. The real aiomqtt.MqttError signals "transient
# reconnect" to both sides; we use one common type so the publisher's
# reconnect loop and the subscriber's backoff loop treat broker outages
# identically.
# ---------------------------------------------------------------------------


class _SimMqttError(Exception):
    """Stand-in for aiomqtt.MqttError used by both fake clients."""


# ---------------------------------------------------------------------------
# In-process broker
# ---------------------------------------------------------------------------


def _topic_matches(filter_topic: str, topic: str) -> bool:
    """Mosquitto-style topic filter match — supports ``+`` and ``#``."""
    f = filter_topic.split("/")
    t = topic.split("/")
    i = 0
    for level in f:
        if level == "#":
            return True
        if i >= len(t):
            return False
        if level != "+" and level != t[i]:
            return False
        i += 1
    return i == len(t)


@dataclass
class _InProcBroker:
    """Pub/sub broker double driven by the test event loop.

    A publisher calls :meth:`publish`; the broker writes the message to
    every subscriber whose filter matches the topic. Retained messages
    are remembered per topic and replayed to every fresh subscriber on
    ``subscribe`` (matches mosquitto's retained-message contract — that
    is what makes the LWT online/offline flip work for late joiners).

    ``dead`` flips the entire broker offline: every connect attempt
    raises :class:`_SimMqttError`, every publish raises, and every
    in-progress message iteration on a subscriber raises. Calling
    :meth:`revive` flips it back and re-delivers retained messages to
    any subscriber that reconnects.
    """

    received: List[Tuple[str, bytes]] = field(default_factory=list)
    retained: Dict[str, Tuple[bytes, int]] = field(default_factory=dict)
    dead: bool = False
    # Queue per subscriber session. Cleared on broker death so a
    # reconnecting subscriber starts from retained + new arrivals.
    _subscribers: List["_FakeSubscriberClient"] = field(default_factory=list)

    def attach(self, sub: "_FakeSubscriberClient") -> None:
        self._subscribers.append(sub)

    def detach(self, sub: "_FakeSubscriberClient") -> None:
        try:
            self._subscribers.remove(sub)
        except ValueError:
            pass

    async def publish(
        self, topic: str, payload: bytes, qos: int, retain: bool
    ) -> None:
        if self.dead:
            raise _SimMqttError("publish: broker down")
        self.received.append((topic, payload))
        # An empty retained payload clears the slot (mosquitto contract).
        if retain:
            if payload == b"":
                self.retained.pop(topic, None)
            else:
                self.retained[topic] = (payload, qos)
        for sub in list(self._subscribers):
            sub.deliver(topic, payload)

    def fire_lwt(self, edge_id: str, payload: bytes) -> None:
        """Simulate an ungraceful disconnect: broker auto-publishes
        the will. Used by the LWT test to model a TCP-RST scenario
        without actually faking the publisher's death."""
        topic = lwt_topic(edge_id)
        self.retained[topic] = (payload, 1)
        for sub in list(self._subscribers):
            sub.deliver(topic, payload)

    def kill(self) -> None:
        self.dead = True
        for sub in list(self._subscribers):
            sub.disconnect()

    def revive(self) -> None:
        self.dead = False


# ---------------------------------------------------------------------------
# Edge-side fake client. MqttTransport calls publish/__aenter__/__aexit__
# on whatever the factory returns; we satisfy that interface.
# ---------------------------------------------------------------------------


class _FakePublisherClient:
    def __init__(self, broker: _InProcBroker) -> None:
        self._broker = broker
        self.entered = False

    async def __aenter__(self) -> "_FakePublisherClient":
        if self._broker.dead:
            raise _SimMqttError("connect refused")
        self.entered = True
        return self

    async def __aexit__(self, *_a) -> bool:
        self.entered = False
        return False

    async def publish(
        self,
        topic: str,
        payload: bytes = b"",
        qos: int = 0,
        retain: bool = False,
        **_kw: Any,
    ) -> None:
        if self._broker.dead or not self.entered:
            raise _SimMqttError("publish: broker down")
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        await self._broker.publish(topic, payload or b"", qos, retain)


# ---------------------------------------------------------------------------
# Center-side fake subscriber client. Holds one message queue per
# subscribed wildcard; ``run_subscriber`` iterates ``client.messages``.
# ---------------------------------------------------------------------------


@dataclass
class _FakeMessage:
    topic: str
    payload: bytes


class _FakeSubscriberClient:
    """Async-context-manager + async iterator stand-in for aiomqtt.Client.

    Holds a list of subscribed filters and an asyncio.Queue of messages
    that match any of them. ``deliver`` is called by the broker when a
    publish lands; it enqueues only when the topic matches one of the
    installed filters. Mosquitto re-delivers retained messages on every
    subscribe, so :meth:`subscribe` synthesises those into the queue.
    """

    def __init__(self, broker: _InProcBroker) -> None:
        self._broker = broker
        self._filters: List[str] = []
        self._queue: asyncio.Queue = asyncio.Queue()
        self._dropped = False

    async def __aenter__(self) -> "_FakeSubscriberClient":
        if self._broker.dead:
            raise _SimMqttError("connect refused")
        self._broker.attach(self)
        return self

    async def __aexit__(self, *_a) -> bool:
        self._broker.detach(self)
        return False

    async def subscribe(self, topic: str, qos: int = 0) -> None:
        if topic not in self._filters:
            self._filters.append(topic)
        # Replay retained messages that match this filter.
        for retained_topic, (payload, _qos) in list(self._broker.retained.items()):
            if _topic_matches(topic, retained_topic):
                self._queue.put_nowait(_FakeMessage(retained_topic, payload))

    def deliver(self, topic: str, payload: bytes) -> None:
        if any(_topic_matches(f, topic) for f in self._filters):
            self._queue.put_nowait(_FakeMessage(topic, payload))

    def disconnect(self) -> None:
        """Broker death: tear the session and unblock any pending iter."""
        self._dropped = True
        self._queue.put_nowait(_SimMqttError("session dropped"))

    @property
    def messages(self):
        return self._iter()

    async def _iter(self):
        while True:
            item = await self._queue.get()
            if isinstance(item, Exception):
                raise item
            yield item


# ---------------------------------------------------------------------------
# Common fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def patch_backoff(monkeypatch):
    """Drop all reconnect backoffs to near-zero so the suite is fast."""
    import edge_agent.transport.mqtt_client as mqc

    monkeypatch.setattr(mqtt_transport, "_BACKOFF_INITIAL", 0.0)
    monkeypatch.setattr(mqtt_transport, "_BACKOFF_MAX", 0.0)
    monkeypatch.setattr(mqc, "_RECONNECT_BACKOFF_INITIAL", 0.0)
    monkeypatch.setattr(mqc, "_RECONNECT_BACKOFF_MAX", 0.0)


@pytest.fixture
def broker() -> _InProcBroker:
    return _InProcBroker()


def _make_publisher(broker: _InProcBroker, edge_id: str) -> MqttTransport:
    transport = MqttTransport(
        broker_url="mqtt://sim:1883",
        edge_id=edge_id,
        client_factory=lambda: _FakePublisherClient(broker),
    )
    transport._mqtt_error_type = lambda: (_SimMqttError,)
    return transport


def _make_subscriber_task(
    broker: _InProcBroker,
    router: UplinkRouter,
) -> Tuple[_FakeSubscriberClient, asyncio.Task]:
    """Spin up ``run_subscriber`` against a fresh fake client."""
    client = _FakeSubscriberClient(broker)
    config = MqttTransportConfig(host="ignored", port=0)
    task = asyncio.create_task(
        run_subscriber(
            config,
            router,
            client_factory=lambda: client,
            reconnect_exceptions=(_SimMqttError,),
        )
    )
    return client, task


async def _await_subscribed(
    broker: _InProcBroker,
    client: _FakeSubscriberClient,
    *,
    deadline: float = 1.0,
) -> None:
    """Spin the event loop until the subscriber is attached AND has installed
    both wildcard subscriptions. Publishing before this would lose frames
    because they'd hit the broker with no live subscriber to deliver to."""
    loop = asyncio.get_event_loop()
    stop = loop.time() + deadline
    while loop.time() < stop:
        if client in broker._subscribers and len(client._filters) >= 2:
            return
        await asyncio.sleep(0)
    raise AssertionError(
        f"subscriber did not attach/subscribe within {deadline:.1f}s "
        f"(attached={client in broker._subscribers}, "
        f"filters={client._filters})"
    )


async def _drain_router(
    drained: List[tuple],
    expected: int,
    *,
    deadline: float = 1.0,
) -> None:
    """Yield until ``drained`` has at least ``expected`` items or the
    deadline elapses. Test failures from missing dispatch should print
    the partial state, not hang forever."""
    loop = asyncio.get_event_loop()
    stop = loop.time() + deadline
    while len(drained) < expected and loop.time() < stop:
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# 1. Dual edge topic isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dual_edge_topic_isolation_no_crosstalk(
    patch_backoff, broker
):
    """Two edges on the same broker publish independently — no cross-talk.

    edge-a fires 5 sample_batch frames at seq 1..5; edge-b fires 5
    at seq 1..5 (each edge owns its own outbox / counter). The center
    router must see exactly 10 dispatched frames, 5 tagged ``edge-a``
    and 5 tagged ``edge-b``, with each edge's frames in seq order and
    no payload from one showing up under the other's edge_id.
    """
    dispatched: List[Tuple[str, Dict[str, Any]]] = []
    router = UplinkRouter()

    async def handler(edge: str, frame: Dict[str, Any]) -> None:
        dispatched.append((edge, frame))

    router.set_handler(handler)
    _sub, sub_task = _make_subscriber_task(broker, router)
    await _await_subscribed(broker, _sub)

    # Two edges, two separate outboxes.
    with tempfile.TemporaryDirectory() as tmp:
        outbox_a = DurableOutbox(str(Path(tmp) / "a.db"))
        outbox_b = DurableOutbox(str(Path(tmp) / "b.db"))
        transport_a = _make_publisher(broker, "edge-a")
        transport_b = _make_publisher(broker, "edge-b")
        await transport_a.connect()
        await transport_b.connect()

        # Stage 5 frames per outbox; each one rides on its edge's own
        # transport so the topic is ``edge/<this-edge>/uplink/...``.
        for outbox, edge in [(outbox_a, "edge-a"), (outbox_b, "edge-b")]:
            for i in range(5):
                outbox.append(lambda seq, edge=edge: {
                    "type": "sample_batch",
                    "edge_id": edge,
                    "monotonic_seq": seq,
                    "task_id": 1,
                    "samples": [{"point_code": "p", "value": i}],
                })

        for seq, frame in outbox_a.pending(after=0):
            await transport_a.publish(frame)
            outbox_a.ack(seq)
        for seq, frame in outbox_b.pending(after=0):
            await transport_b.publish(frame)
            outbox_b.ack(seq)

        # 2 LWT online + 5 sample_batch per edge = 12 dispatched in total.
        await _drain_router(dispatched, expected=12)

        await transport_a.close()
        await transport_b.close()

    sub_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sub_task

    # Assertions ----------------------------------------------------------
    # Split LWT presence frames from the sample-batch data stream; only
    # the data stream is the cross-talk concern.
    samples = [(e, f) for e, f in dispatched
               if f.get("type") == "sample_batch"]
    assert len(samples) == 10, [d[0] for d in samples]

    by_edge: Dict[str, List[Dict[str, Any]]] = {"edge-a": [], "edge-b": []}
    for edge, frame in samples:
        by_edge.setdefault(edge, []).append(frame)

    # No edge picked up the other's traffic.
    assert set(by_edge) == {"edge-a", "edge-b"}, by_edge.keys()
    assert len(by_edge["edge-a"]) == 5
    assert len(by_edge["edge-b"]) == 5

    # Each edge's frames carry only its own edge_id in the payload — i.e.
    # the publisher's own topic prefix is what the router saw, never the
    # peer's. (The center's subscriber tags edge_name from the topic, not
    # the payload, so a frame mis-routed would show up under the wrong key.)
    assert all(f["edge_id"] == "edge-a" for f in by_edge["edge-a"])
    assert all(f["edge_id"] == "edge-b" for f in by_edge["edge-b"])

    # Per-edge seqs strictly increasing 1..5. No interleaving lost the order.
    assert [f["monotonic_seq"] for f in by_edge["edge-a"]] == [1, 2, 3, 4, 5]
    assert [f["monotonic_seq"] for f in by_edge["edge-b"]] == [1, 2, 3, 4, 5]

    # Broker-side topic check: no edge-a frame landed on edge-b's topic
    # tree or vice versa.
    topics = {topic for topic, _ in broker.received}
    a_uplink = {t for t in topics if t.startswith("edge/edge-a/uplink/")}
    b_uplink = {t for t in topics if t.startswith("edge/edge-b/uplink/")}
    assert a_uplink and b_uplink
    assert not any(t.startswith("edge/edge-b/") for t in a_uplink)
    assert not any(t.startswith("edge/edge-a/") for t in b_uplink)


# ---------------------------------------------------------------------------
# 2. QoS 1 restart resume
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qos1_restart_resumes_seq_no_gap_no_duplicate(
    patch_backoff, broker
):
    """Restart the edge mid-stream — broker keeps a contiguous seq stream.

    Phase 2 P3 (XIU-102) / P4 (XIU-103) require the durable outbox's seq
    counter to survive a process restart and the QoS 1 PUBACK flow to
    produce zero duplicates when the publisher re-connects with the same
    counter. We model the restart as ``transport.close()`` →
    ``transport.connect()`` against a brand-new MqttTransport that points
    at the **same** outbox file (since the seq counter lives in there).
    """
    dispatched: List[Tuple[str, Dict[str, Any]]] = []
    router = UplinkRouter()

    async def handler(edge: str, frame: Dict[str, Any]) -> None:
        dispatched.append((edge, frame))

    router.set_handler(handler)
    _sub, sub_task = _make_subscriber_task(broker, router)
    await _await_subscribed(broker, _sub)

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "edge_state.db")
        outbox = DurableOutbox(db_path)

        # ---- session 1 — publish seq 1..3 -------------------------------
        transport_v1 = _make_publisher(broker, "edge-restart")
        await transport_v1.connect()
        for _ in range(3):
            outbox.append(lambda seq: {
                "type": "sample_batch", "edge_id": "edge-restart",
                "monotonic_seq": seq, "task_id": 1, "samples": [],
            })
        for seq, frame in outbox.pending(after=0):
            await transport_v1.publish(frame)
            outbox.ack(seq)
        await transport_v1.close()

        # ---- session 2 — restart against the SAME outbox file ----------
        # The seq counter must continue at 4. A fresh DurableOutbox handle
        # over the same file is the closest analogue to "process restart".
        outbox_v2 = DurableOutbox(db_path)
        assert outbox_v2.seq_high() == 3, (
            "seq counter must survive a restart (P4 — XIU-103)"
        )

        transport_v2 = _make_publisher(broker, "edge-restart")
        await transport_v2.connect()
        for _ in range(3):
            outbox_v2.append(lambda seq: {
                "type": "sample_batch", "edge_id": "edge-restart",
                "monotonic_seq": seq, "task_id": 1, "samples": [],
            })
        for seq, frame in outbox_v2.pending(after=0):
            await transport_v2.publish(frame)
            outbox_v2.ack(seq)
        await transport_v2.close()

        await _drain_router(dispatched, expected=6)

    sub_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sub_task

    # All six frames reached the router. Restart is invisible end-to-end.
    sample_frames = [
        (e, f) for e, f in dispatched if f.get("type") == "sample_batch"
    ]
    assert len(sample_frames) == 6
    seqs = [f["monotonic_seq"] for _, f in sample_frames]
    assert seqs == [1, 2, 3, 4, 5, 6], (
        f"seq stream must be contiguous (no gap, no duplicate); got {seqs}"
    )
    assert len(set(seqs)) == len(seqs), "no duplicate seq"
    assert all(e == "edge-restart" for e, _ in sample_frames)


# ---------------------------------------------------------------------------
# 3. LWT — status flip via presence handler
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_db_defaults():
    """Mirror test_mqtt_lwt.py — re-inject DB defaults the session fixture
    drops, so ``database_sync_to_async`` workers don't KeyError."""
    from django.conf import settings

    db = settings.DATABASES["default"]
    db.setdefault("TIME_ZONE", None)
    db.setdefault("CONN_HEALTH_CHECKS", False)
    db.setdefault("CONN_MAX_AGE", 0)
    db.setdefault("AUTOCOMMIT", True)
    db.setdefault("OPTIONS", {})
    yield


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_lwt_online_flips_edge_status_to_online(patch_backoff, broker):
    """Edge connect → online retained → center subscriber → presence flip."""
    from asgiref.sync import sync_to_async

    from fleet.models import EdgeNode, EdgeStatus

    edge, _ = await sync_to_async(EdgeNode.issue)(name="edge-lwt-online")

    # Wire LWT type handler on a fresh router so we don't taint the
    # process-wide singleton other tests use.
    router = UplinkRouter()
    presence.install(router)
    _sub, sub_task = _make_subscriber_task(broker, router)
    await _await_subscribed(broker, _sub)

    transport = _make_publisher(broker, "edge-lwt-online")
    try:
        await transport.connect()
        # The transport's _open_session publishes online retained.
        # Yield a few times so the subscriber drains the retained message.
        for _ in range(40):
            await asyncio.sleep(0)
            refreshed = await sync_to_async(
                EdgeNode.objects.get
            )(name="edge-lwt-online")
            if refreshed.status == EdgeStatus.ONLINE:
                break
        assert refreshed.status == EdgeStatus.ONLINE
        assert refreshed.last_seen is not None
    finally:
        await transport.close()
        sub_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sub_task


@pytest.mark.asyncio
@pytest.mark.django_db
async def test_broker_will_flips_edge_status_to_offline(
    patch_backoff, broker
):
    """Broker WILL fires (ungraceful disconnect) → EdgeNode → OFFLINE.

    The mosquitto WILL behaviour is: when the broker detects a TCP RST
    or keepalive timeout on a connection that registered a WILL, the
    broker itself publishes the WILL payload on the configured topic.
    We model that with :meth:`_InProcBroker.fire_lwt` — it is the
    broker's job, not the edge's, so the publisher can be torn down or
    crashed without it ever sending the offline frame.
    """
    from asgiref.sync import sync_to_async

    from fleet.models import EdgeLifecycleEvent, EdgeNode, EdgeStatus

    edge, _ = await sync_to_async(EdgeNode.issue)(name="edge-lwt-offline")
    # Pre-mark online so the OFFLINE transition is observable.
    await sync_to_async(edge.mark_online)(version="0.1.0")

    router = UplinkRouter()
    presence.install(router)
    _sub, sub_task = _make_subscriber_task(broker, router)
    await _await_subscribed(broker, _sub)

    # Broker fires the WILL — represents the edge having TCP-RST'd.
    will_payload = json.dumps(
        {"state": "offline", "edge_id": "edge-lwt-offline"}
    ).encode()
    broker.fire_lwt("edge-lwt-offline", will_payload)

    try:
        for _ in range(60):
            await asyncio.sleep(0)
            refreshed = await sync_to_async(
                EdgeNode.objects.get
            )(name="edge-lwt-offline")
            if refreshed.status == EdgeStatus.OFFLINE:
                break
        assert refreshed.status == EdgeStatus.OFFLINE

        rows = await sync_to_async(
            lambda: list(EdgeLifecycleEvent.objects.filter(
                edge=refreshed, event="session.offline"
            ))
        )()
        # Exactly one session.offline lifecycle row appended.
        assert len(rows) == 1, rows
    finally:
        sub_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sub_task


# ---------------------------------------------------------------------------
# Optional live broker mode — set FLEET_MQTT_TEST_BROKER=host[:port] to
# run the same scenarios against a real mosquitto. Skipped by default so
# CI doesn't need docker. The chaos script
# (scripts/chaos_mqtt_broker_kill.py --live) is the broader live smoke.
# ---------------------------------------------------------------------------


def _live_broker_addr():
    spec = os.environ.get("FLEET_MQTT_TEST_BROKER")
    if not spec:
        return None
    host, _, port = spec.partition(":")
    return host, int(port or 1883)


@pytest.mark.asyncio
async def test_live_broker_dual_edge_topic_isolation():
    """Dual-edge isolation against a real mosquitto. Opt-in via env var."""
    addr = _live_broker_addr()
    if addr is None:
        pytest.skip(
            "FLEET_MQTT_TEST_BROKER not set — run with "
            "FLEET_MQTT_TEST_BROKER=127.0.0.1:1883 after "
            "`docker compose -f docker-compose.center.yml up -d mosquitto`"
        )
    aiomqtt = pytest.importorskip("aiomqtt")
    host, port = addr

    seen_a: List[int] = []
    seen_b: List[int] = []

    async def collect():
        async with aiomqtt.Client(
            hostname=host, port=port,
            identifier=f"qa-xiu104-sub-{os.getpid()}",
        ) as sub:
            await sub.subscribe("edge/qa-xiu104-a/uplink/#", qos=1)
            await sub.subscribe("edge/qa-xiu104-b/uplink/#", qos=1)
            async for msg in sub.messages:
                body = json.loads(msg.payload.decode())
                seq = int(body.get("monotonic_seq", 0))
                if str(msg.topic).startswith("edge/qa-xiu104-a/"):
                    seen_a.append(seq)
                elif str(msg.topic).startswith("edge/qa-xiu104-b/"):
                    seen_b.append(seq)
                if len(seen_a) >= 3 and len(seen_b) >= 3:
                    return

    collect_task = asyncio.create_task(collect())
    await asyncio.sleep(0.3)  # let the subscriber establish

    async with aiomqtt.Client(
        hostname=host, port=port,
        identifier=f"qa-xiu104-pub-a-{os.getpid()}",
    ) as pub_a, aiomqtt.Client(
        hostname=host, port=port,
        identifier=f"qa-xiu104-pub-b-{os.getpid()}",
    ) as pub_b:
        for seq in range(1, 4):
            await pub_a.publish(
                topic_for("qa-xiu104-a", "sample_batch"),
                payload=json.dumps(
                    {"type": "sample_batch", "edge_id": "qa-xiu104-a",
                     "monotonic_seq": seq}
                ).encode(), qos=1,
            )
            await pub_b.publish(
                topic_for("qa-xiu104-b", "sample_batch"),
                payload=json.dumps(
                    {"type": "sample_batch", "edge_id": "qa-xiu104-b",
                     "monotonic_seq": seq}
                ).encode(), qos=1,
            )

    try:
        await asyncio.wait_for(collect_task, timeout=5.0)
    except asyncio.TimeoutError:
        collect_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await collect_task
        pytest.fail(f"timeout — seen_a={seen_a} seen_b={seen_b}")

    assert seen_a == [1, 2, 3], seen_a
    assert seen_b == [1, 2, 3], seen_b
