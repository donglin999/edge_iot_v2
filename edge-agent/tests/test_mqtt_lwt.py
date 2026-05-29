"""Edge LWT + online presence publish — Phase 2 P4 (XIU-103).

What we cover here:

1. ``_build_client`` passes ``will=Will(edge/<id>/lwt, ..., retain=True)``
   to the aiomqtt.Client constructor so the broker auto-publishes the
   offline retained payload on ungraceful disconnect.
2. ``_open_session`` publishes the matching ``{state: online}`` retained
   payload on every successful connect — so the broker's last retained
   value tracks the edge's true presence.
3. ``close()`` publishes a final ``{state: offline}`` retained payload
   before disconnecting so a planned shutdown doesn't leave the broker
   stuck on the previous ``online`` retained value.
4. A publish failure during online-LWT push tears the half-open session
   back down and retries; the broker is never left thinking we're
   online when we cannot actually publish.

The fake client mirrors the one used by :mod:`test_mqtt_uplink` so the
two cover overlapping shapes without re-importing.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, List, Optional

import pytest

from edge_agent.transport.mqtt_client import (
    LWT_STATE_OFFLINE,
    LWT_STATE_ONLINE,
    MqttTransport,
    _lwt_payload,
    lwt_topic,
)


@dataclass
class _PublishCall:
    topic: str
    payload: bytes
    qos: int
    retain: bool


@dataclass
class _FakeAiomqttClient:
    """Async context manager + publish recorder for LWT tests.

    Tracks every publish call (topic / payload / qos / retain) and the
    enter/exit lifecycle so a test can assert "online published exactly
    once on connect" or "session torn down after a failed online push".
    """

    enter_count: int = 0
    exit_count: int = 0
    publish_calls: List[_PublishCall] = field(default_factory=list)
    publish_errors: List[Optional[Exception]] = field(default_factory=list)

    async def __aenter__(self):
        self.enter_count += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.exit_count += 1
        return False

    async def publish(self, topic, payload=None, qos=0, retain=False, **_kw):
        if self.publish_errors:
            exc = self.publish_errors.pop(0)
            if exc is not None:
                raise exc
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.publish_calls.append(
            _PublishCall(
                topic=str(topic),
                payload=bytes(payload or b""),
                qos=int(qos),
                retain=bool(retain),
            )
        )


def test_lwt_topic_matches_center_subscription():
    """Sanity: the edge topic mirrors what the center subscriber listens on."""
    assert lwt_topic("edge-1") == "edge/edge-1/lwt"
    assert lwt_topic("device-77") == "edge/device-77/lwt"


def test_lwt_payload_shape():
    """Payload carries the state, the edge_id echo, and an ISO timestamp."""
    raw = _lwt_payload("edge-1", LWT_STATE_OFFLINE)
    body = json.loads(raw.decode("utf-8"))
    assert body["state"] == "offline"
    assert body["edge_id"] == "edge-1"
    assert "ts" in body and body["ts"]


@pytest.mark.asyncio
async def test_will_passes_offline_payload_to_aiomqtt(monkeypatch):
    """``_build_client`` configures the WILL with the retained offline LWT.

    We monkeypatch ``aiomqtt.Client`` so the test captures the kwargs it
    was constructed with — that proves the WILL is wired without needing
    a live broker.
    """
    aiomqtt = pytest.importorskip("aiomqtt")

    captured: dict = {}

    real_will = aiomqtt.Will

    def _capture_client(**kw):
        captured.update(kw)
        return _FakeAiomqttClient()

    monkeypatch.setattr(aiomqtt, "Client", _capture_client)

    transport = MqttTransport(broker_url="mqtt://broker:1883", edge_id="edge-7")
    # Use the production path (no client_factory injection) so the WILL
    # construction happens inside the transport.
    client = transport._build_client()
    assert client is not None

    will = captured.get("will")
    assert will is not None and isinstance(will, real_will)
    assert will.topic == "edge/edge-7/lwt"
    assert will.qos == 1
    assert will.retain is True
    body = json.loads(will.payload.decode("utf-8"))
    assert body["state"] == "offline"
    assert body["edge_id"] == "edge-7"


@pytest.mark.asyncio
async def test_connect_publishes_online_retained():
    """The first thing a fresh session does is publish online retained."""
    client = _FakeAiomqttClient()
    transport = MqttTransport(
        broker_url="mqtt://broker:1883",
        edge_id="edge-1",
        client_factory=lambda: client,
    )
    await transport.connect()

    online_calls = [
        c for c in client.publish_calls if c.topic == "edge/edge-1/lwt"
    ]
    assert len(online_calls) == 1
    call = online_calls[0]
    assert call.qos == 1
    assert call.retain is True
    body = json.loads(call.payload.decode("utf-8"))
    assert body["state"] == "online"
    assert body["edge_id"] == "edge-1"


@pytest.mark.asyncio
async def test_close_publishes_offline_retained_then_disconnects():
    """Graceful close replaces the retained value with ``offline`` before exit."""
    client = _FakeAiomqttClient()
    transport = MqttTransport(
        broker_url="mqtt://broker:1883",
        edge_id="edge-9",
        client_factory=lambda: client,
    )
    await transport.connect()
    online_count = len(client.publish_calls)
    await transport.close()

    # The close path adds exactly one extra publish: the offline LWT.
    assert len(client.publish_calls) == online_count + 1
    last = client.publish_calls[-1]
    assert last.topic == "edge/edge-9/lwt"
    assert last.qos == 1
    assert last.retain is True
    body = json.loads(last.payload.decode("utf-8"))
    assert body["state"] == "offline"
    # The aiomqtt session was actually exited.
    assert client.exit_count == 1


@pytest.mark.asyncio
async def test_online_publish_failure_drops_session_and_retries():
    """A failing online-LWT publish must NOT leave a half-open session.

    Otherwise the broker would hold the prior ``offline`` retained
    payload while the agent thought it was online — exactly the
    failure mode P4 is meant to eliminate.
    """

    class _FakeMqttError(Exception):
        pass

    first = _FakeAiomqttClient(publish_errors=[_FakeMqttError("publish drop")])
    second = _FakeAiomqttClient()
    clients = iter([first, second])
    transport = MqttTransport(
        broker_url="mqtt://broker:1883",
        edge_id="edge-x",
        client_factory=lambda: next(clients),
    )
    transport._mqtt_error_type = lambda: (_FakeMqttError,)
    # Drop the inter-attempt sleep so the test runs instantly.
    import edge_agent.transport.mqtt_client as mqc

    # Patch the module-level constants for the duration of this test.
    saved_initial = mqc._RECONNECT_BACKOFF_INITIAL
    saved_max = mqc._RECONNECT_BACKOFF_MAX
    mqc._RECONNECT_BACKOFF_INITIAL = 0.0
    mqc._RECONNECT_BACKOFF_MAX = 0.0
    try:
        await asyncio.wait_for(transport.connect(), timeout=1.0)
    finally:
        mqc._RECONNECT_BACKOFF_INITIAL = saved_initial
        mqc._RECONNECT_BACKOFF_MAX = saved_max

    # The first client was entered + then exited cleanly.
    assert first.enter_count == 1
    assert first.exit_count == 1
    # The second one is the one we ended up with — and it got the
    # online-LWT publish.
    assert second.enter_count == 1
    online_calls = [
        c for c in second.publish_calls if c.topic == "edge/edge-x/lwt"
    ]
    assert len(online_calls) == 1
    assert json.loads(online_calls[0].payload.decode("utf-8"))["state"] == "online"


@pytest.mark.asyncio
async def test_close_tolerates_broker_already_gone():
    """If the offline publish raises (broker dead), close still completes.

    The broker's WILL fires when the TCP socket reaps, so dropping the
    explicit offline publish on the floor is the right move — we must
    not block shutdown waiting for an unreachable broker.
    """

    class _FakeMqttError(Exception):
        pass

    client = _FakeAiomqttClient()
    transport = MqttTransport(
        broker_url="mqtt://broker:1883",
        edge_id="edge-z",
        client_factory=lambda: client,
    )
    transport._mqtt_error_type = lambda: (_FakeMqttError,)
    await transport.connect()
    # Now arm the next publish (the close-time offline publish) to fail.
    client.publish_errors.append(_FakeMqttError("broker gone"))

    # close() must still complete and exit the underlying session.
    await asyncio.wait_for(transport.close(), timeout=1.0)
    assert client.exit_count == 1
    assert transport._client is None
