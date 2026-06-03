"""Background presence keepalive — XIU-129.

Regression coverage for the idle-edge-stuck-offline bug: an edge with no
AcqTask never publishes, so before this fix a ``persistence false`` broker
restart wiped the retained ``online`` and nothing on the edge ever noticed
the dead session or re-announced — the pure-LWT center (XIU-112) then
wedged the edge ``offline`` forever.

The fix is a background timer in :class:`MqttTransport` that re-publishes
``edge/<id>/lwt={online}`` retained on a fixed cadence and reconnects when
that probe publish fails. These tests exercise it with a tiny interval and
an injected fake client so no real broker / wall-clock wait is needed.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, List, Optional

import pytest

from edge_agent.transport.mqtt_client import (
    LWT_STATE_ONLINE,
    MqttTransport,
)


class _FakeMqttError(Exception):
    pass


@dataclass
class _PublishCall:
    topic: str
    payload: bytes
    qos: int
    retain: bool


@dataclass
class _FakeAiomqttClient:
    """Async-CM publish recorder; can fail enter or the Nth publish.

    ``enter_error`` (if set) makes ``__aenter__`` raise — used to model a
    broker that stays down across reconnect attempts. ``publish_errors`` is
    consumed one-per-publish: a non-``None`` entry raises in place of that
    publish (e.g. the keepalive probe hitting a dead session).
    """

    enter_error: Optional[Exception] = None
    enter_count: int = 0
    exit_count: int = 0
    publish_calls: List[_PublishCall] = field(default_factory=list)
    publish_errors: List[Optional[Exception]] = field(default_factory=list)

    async def __aenter__(self):
        self.enter_count += 1
        if self.enter_error is not None:
            raise self.enter_error
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
            _PublishCall(str(topic), bytes(payload or b""), int(qos), bool(retain))
        )

    def online_calls(self, edge_id: str) -> List[_PublishCall]:
        topic = f"edge/{edge_id}/lwt"
        return [
            c
            for c in self.publish_calls
            if c.topic == topic
            and json.loads(c.payload.decode("utf-8")).get("state") == LWT_STATE_ONLINE
        ]


@pytest.mark.asyncio
async def test_presence_loop_reasserts_online_periodically():
    """An idle session re-publishes ``online`` retained on every tick."""
    client = _FakeAiomqttClient()
    transport = MqttTransport(
        broker_url="mqtt://broker:1883",
        edge_id="idle-1",
        client_factory=lambda: client,
        presence_interval=0.02,
    )
    transport._mqtt_error_type = lambda: (_FakeMqttError,)
    await transport.connect()
    # connect() itself publishes online exactly once.
    assert len(client.online_calls("idle-1")) == 1

    # Let the keepalive fire a few times without any uplink publish at all —
    # this is the idle edge that the old publish-driven path never serviced.
    await asyncio.sleep(0.1)
    await transport.close()

    # >1 means the background timer (not connect) drove the extra re-asserts.
    assert len(client.online_calls("idle-1")) > 1
    # Every keepalive publish is retained QoS 1 so the broker's last value
    # is always a live ``online``.
    for call in client.online_calls("idle-1"):
        assert call.qos == 1 and call.retain is True


@pytest.mark.asyncio
async def test_presence_probe_failure_reconnects_and_reannounces():
    """The core XIU-129 repro: broker bounced under an *idle* edge.

    The first session connects + announces online, then the next keepalive
    probe fails (the session died when the broker restarted). The transport
    must drop the dead session and reconnect — re-publishing the retained
    ``online`` the broker dropped on its ``persistence false`` restart — with
    no uplink publish ever happening.
    """
    # client1: connect-online OK, then keepalive probe fails (dead session).
    client1 = _FakeAiomqttClient(publish_errors=[None, _FakeMqttError("broker bounced")])
    # client2: the post-restart reconnect — announces online cleanly.
    client2 = _FakeAiomqttClient()
    clients = iter([client1, client2])
    transport = MqttTransport(
        broker_url="mqtt://broker:1883",
        edge_id="idle-2",
        client_factory=lambda: next(clients),
        presence_interval=0.02,
    )
    transport._mqtt_error_type = lambda: (_FakeMqttError,)

    import edge_agent.transport.mqtt_client as mqc

    saved_initial, saved_max = mqc._RECONNECT_BACKOFF_INITIAL, mqc._RECONNECT_BACKOFF_MAX
    mqc._RECONNECT_BACKOFF_INITIAL = mqc._RECONNECT_BACKOFF_MAX = 0.0
    try:
        await transport.connect()
        assert len(client1.online_calls("idle-2")) == 1
        # Wait long enough for the probe to fail + the reconnect to land.
        for _ in range(50):
            if client2.online_calls("idle-2"):
                break
            await asyncio.sleep(0.02)
        await transport.close()
    finally:
        mqc._RECONNECT_BACKOFF_INITIAL = saved_initial
        mqc._RECONNECT_BACKOFF_MAX = saved_max

    # The dead session was torn down...
    assert client1.exit_count >= 1
    # ...and the reconnect re-announced online on the fresh session — the
    # edge is back to ``online`` on the broker without ever sending uplink.
    assert len(client2.online_calls("idle-2")) >= 1


@pytest.mark.asyncio
async def test_close_does_not_deadlock_while_reconnecting():
    """close() must cancel a keepalive parked in reconnect backoff.

    A keepalive that fails its probe enters ``_open_session``'s infinite
    reconnect loop holding ``self._lock``. ``close`` cancels the task before
    taking the lock, so a broker that never comes back cannot wedge shutdown.
    """
    # client1 connects; its keepalive probe then fails.
    client1 = _FakeAiomqttClient(publish_errors=[None, _FakeMqttError("gone")])
    # Every reconnect attempt fails at __aenter__ → _open_session loops forever.
    def _factory():
        if not _factory.first_used:
            _factory.first_used = True
            return client1
        return _FakeAiomqttClient(enter_error=_FakeMqttError("still down"))

    _factory.first_used = False

    transport = MqttTransport(
        broker_url="mqtt://broker:1883",
        edge_id="idle-3",
        client_factory=_factory,
        presence_interval=0.02,
    )
    transport._mqtt_error_type = lambda: (_FakeMqttError,)

    import edge_agent.transport.mqtt_client as mqc

    saved_initial, saved_max = mqc._RECONNECT_BACKOFF_INITIAL, mqc._RECONNECT_BACKOFF_MAX
    mqc._RECONNECT_BACKOFF_INITIAL = mqc._RECONNECT_BACKOFF_MAX = 0.05
    try:
        await transport.connect()
        # Give the probe time to fail and park the task in reconnect backoff.
        await asyncio.sleep(0.1)
        # The whole point: this returns instead of deadlocking on the lock.
        await asyncio.wait_for(transport.close(), timeout=2.0)
    finally:
        mqc._RECONNECT_BACKOFF_INITIAL = saved_initial
        mqc._RECONNECT_BACKOFF_MAX = saved_max

    assert transport._presence_task is None
