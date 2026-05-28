"""MQTT uplink tests — Phase 2 P3 (XIU-102).

Covers the new transport split:

* ``EdgeConfig`` parses ``EDGE_TRANSPORT`` / ``EDGE_MQTT_BROKER`` and
  defaults to ``mqtt`` per the migration plan (§9.4).
* ``MqttTransport`` publishes lifecycle / sample_batch / alarm_event to
  ``edge/<edge_id>/uplink/<frame_type>`` at QoS 1, with the expected
  payload schema preserved across the JSON encode/decode round-trip.
* ``EdgeAgent._uplink_loop`` drains the durable outbox over an injected
  MQTT transport in seq order, prunes acked rows on broker PUBACK, and
  preserves the original frame ordering on the wire.

The test fakes the aiomqtt client (same pattern the center subscriber
tests use in :mod:`backend.tests.transport.test_mqtt_subscribe`) so CI
doesn't need a live Mosquitto broker. A second test exercises the same
flow against a real broker when ``EDGE_MQTT_TEST_BROKER`` is exported,
matching the P1 acceptance smoke.
"""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

import pytest

from edge_agent.agent import EdgeAgent
from edge_agent.config import ConfigError, EdgeConfig
from edge_agent.outbox import DurableOutbox
from edge_agent.transport import MqttTransport, WsTransport
from edge_agent.transport.mqtt_client import _parse_broker_url, topic_for
from edge_agent.__main__ import _build_uplink_transport


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _PublishCall:
    topic: str
    payload: bytes
    qos: int


@dataclass
class _FakeAiomqttClient:
    """Async-context-manager stand-in for ``aiomqtt.Client``.

    Records every ``publish`` call. ``publish`` is a coroutine in the
    real client so we keep the same shape here. Tests can pre-load
    :attr:`publish_errors` with a list of exceptions to raise from the
    *next* publish calls — this drives the reconnect path.
    """

    enter_count: int = 0
    exit_count: int = 0
    closed: bool = False
    publish_calls: List[_PublishCall] = field(default_factory=list)
    publish_errors: List[Optional[Exception]] = field(default_factory=list)

    async def __aenter__(self):
        self.enter_count += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.exit_count += 1
        self.closed = True
        return False

    async def publish(self, topic, payload=None, qos=0, **_kwargs):
        if self.publish_errors:
            exc = self.publish_errors.pop(0)
            if exc is not None:
                raise exc
        # aiomqtt accepts bytes / str — normalise to bytes for assertions.
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.publish_calls.append(
            _PublishCall(topic=str(topic), payload=bytes(payload or b""), qos=int(qos))
        )


def _cfg(**overrides) -> EdgeConfig:
    base = dict(
        edge_id="edge-test", edge_token="tk",
        center_url="ws://test/ws/fleet/", labels={}, log_level="INFO",
    )
    base.update(overrides)
    return EdgeConfig(**base)


# ---------------------------------------------------------------------------
# Config + main wiring
# ---------------------------------------------------------------------------


class TestTransportConfig:
    def test_defaults_to_mqtt(self):
        cfg = EdgeConfig.from_env({
            "EDGE_ID": "e", "EDGE_TOKEN": "t", "CENTER_URL": "ws://c/",
        })
        assert cfg.transport == "mqtt"
        assert cfg.mqtt_broker == "mqtt://mosquitto:1883"

    def test_explicit_ws(self):
        cfg = EdgeConfig.from_env({
            "EDGE_ID": "e", "EDGE_TOKEN": "t", "CENTER_URL": "ws://c/",
            "EDGE_TRANSPORT": "ws",
        })
        assert cfg.transport == "ws"

    def test_custom_broker(self):
        cfg = EdgeConfig.from_env({
            "EDGE_ID": "e", "EDGE_TOKEN": "t", "CENTER_URL": "ws://c/",
            "EDGE_MQTT_BROKER": "mqtts://broker.example.com:8883",
        })
        assert cfg.mqtt_broker == "mqtts://broker.example.com:8883"

    def test_rejects_unknown_transport(self):
        with pytest.raises(ConfigError):
            EdgeConfig.from_env({
                "EDGE_ID": "e", "EDGE_TOKEN": "t", "CENTER_URL": "ws://c/",
                "EDGE_TRANSPORT": "carrier-pigeon",
            })

    def test_build_uplink_transport_mqtt(self):
        cfg = _cfg(transport="mqtt", mqtt_broker="mqtt://broker:1883")
        t = _build_uplink_transport(cfg)
        assert isinstance(t, MqttTransport)

    def test_build_uplink_transport_ws_returns_none(self):
        cfg = _cfg(transport="ws")
        assert _build_uplink_transport(cfg) is None


# ---------------------------------------------------------------------------
# Broker URL + topic helpers
# ---------------------------------------------------------------------------


class TestBrokerUrl:
    def test_default_scheme(self):
        kw = _parse_broker_url("mqtt://mosquitto:1883")
        assert kw == {
            "hostname": "mosquitto", "port": 1883, "tls": False,
            "username": None, "password": None,
        }

    def test_tls_scheme_default_port(self):
        kw = _parse_broker_url("mqtts://broker.example.com")
        assert kw["tls"] is True
        assert kw["port"] == 8883
        assert kw["hostname"] == "broker.example.com"

    def test_bare_host_port(self):
        kw = _parse_broker_url("mosquitto:1883")
        assert kw["hostname"] == "mosquitto"
        assert kw["port"] == 1883
        assert kw["tls"] is False

    def test_credentials(self):
        kw = _parse_broker_url("mqtt://user:pw@host:1884")
        assert (kw["username"], kw["password"]) == ("user", "pw")
        assert kw["port"] == 1884

    def test_topic_for(self):
        assert topic_for("edge-1", "lifecycle") == "edge/edge-1/uplink/lifecycle"
        assert topic_for("edge-2", "sample_batch") == "edge/edge-2/uplink/sample_batch"
        assert topic_for("e3", "alarm_event") == "edge/e3/uplink/alarm_event"


# ---------------------------------------------------------------------------
# MqttTransport per-frame publish
# ---------------------------------------------------------------------------


class TestMqttTransportPublish:
    """Exercise each ``send_*`` method end-to-end through a fake client."""

    @staticmethod
    def _make() -> Tuple[MqttTransport, _FakeAiomqttClient]:
        client = _FakeAiomqttClient()
        transport = MqttTransport(
            broker_url="mqtt://test:1883", edge_id="edge-1",
            client_factory=lambda: client,
        )
        return transport, client

    @pytest.mark.asyncio
    async def test_send_state_publishes_lifecycle_topic(self):
        transport, client = self._make()
        await transport.connect()
        frame = {"type": "lifecycle", "edge_id": "edge-1",
                 "monotonic_seq": 1, "event": "session.online",
                 "ts": "2026-05-28T00:00:00Z"}
        await transport.send_state(frame)
        await transport.close()
        assert len(client.publish_calls) == 1
        call = client.publish_calls[0]
        assert call.topic == "edge/edge-1/uplink/lifecycle"
        assert call.qos == 1
        assert json.loads(call.payload.decode()) == frame

    @pytest.mark.asyncio
    async def test_send_sample_publishes_sample_batch_topic(self):
        transport, client = self._make()
        frame = {
            "type": "sample_batch", "edge_id": "edge-1",
            "monotonic_seq": 7, "task_id": 3, "task_code": "t3",
            "window_start": "2026-05-28T00:00:00Z",
            "window_end": "2026-05-28T00:00:01Z",
            "samples": [{"point_code": "p", "value": 1.0,
                          "quality": "good",
                          "timestamp": "2026-05-28T00:00:00.500Z"}],
        }
        await transport.send_sample(frame)
        assert client.publish_calls[0].topic == "edge/edge-1/uplink/sample_batch"
        assert client.publish_calls[0].qos == 1
        assert json.loads(client.publish_calls[0].payload.decode()) == frame

    @pytest.mark.asyncio
    async def test_send_alarm_publishes_alarm_event_topic(self):
        transport, client = self._make()
        frame = {
            "type": "alarm_event", "edge_id": "edge-1", "monotonic_seq": 9,
            "rule_id": 11, "point_code": "p1", "device_code": "d1",
            "value": 42, "severity": "critical", "status": "firing",
            "message": "boom", "fired_at": "2026-05-28T00:00:01Z",
        }
        await transport.send_alarm(frame)
        assert client.publish_calls[0].topic == "edge/edge-1/uplink/alarm_event"
        assert json.loads(client.publish_calls[0].payload.decode()) == frame

    @pytest.mark.asyncio
    async def test_generic_publish_dispatches_by_type(self):
        transport, client = self._make()
        frames = [
            {"type": "lifecycle", "event": "session.online", "monotonic_seq": 1},
            {"type": "sample_batch", "samples": [], "monotonic_seq": 2},
            {"type": "alarm_event", "rule_id": 1, "monotonic_seq": 3},
        ]
        for f in frames:
            await transport.publish(f)
        topics = [c.topic for c in client.publish_calls]
        assert topics == [
            "edge/edge-1/uplink/lifecycle",
            "edge/edge-1/uplink/sample_batch",
            "edge/edge-1/uplink/alarm_event",
        ]

    @pytest.mark.asyncio
    async def test_publish_rejects_unknown_frame_type(self):
        transport, _ = self._make()
        with pytest.raises(ValueError):
            await transport.publish({"type": "heartbeat"})

    @pytest.mark.asyncio
    async def test_confirms_on_publish_flag(self):
        # The agent uses this attribute to decide whether to prune the
        # outbox row after a successful publish — see ``_uplink_loop``.
        transport, _ = self._make()
        assert transport.confirms_on_publish is True
        assert WsTransport(None).confirms_on_publish is False

    @pytest.mark.asyncio
    async def test_publish_resets_session_on_mqtt_error(self):
        """A failing publish clears the session so the next one reconnects."""

        class _FakeMqttError(Exception):
            pass

        broken = _FakeAiomqttClient(publish_errors=[_FakeMqttError("drop")])
        healthy = _FakeAiomqttClient()
        clients = iter([broken, healthy])
        transport = MqttTransport(
            broker_url="mqtt://test:1883", edge_id="edge-1",
            client_factory=lambda: next(clients),
        )
        # Steer the mqtt-error resolver away from a real aiomqtt import.
        transport._mqtt_error_type = lambda: (_FakeMqttError,)

        with pytest.raises(_FakeMqttError):
            await transport.send_state({"type": "lifecycle", "monotonic_seq": 1})
        assert transport._client is None
        # Next publish reuses a fresh session and succeeds.
        await transport.send_state({"type": "lifecycle", "monotonic_seq": 2})
        assert len(healthy.publish_calls) == 1


# ---------------------------------------------------------------------------
# End-to-end: outbox → uplink loop → mock subscriber
# ---------------------------------------------------------------------------


class TestUplinkLoopOverMqtt:
    """Drive ``EdgeAgent._uplink_loop`` over a fake MQTT transport.

    The fake transport stands in for ``MqttTransport`` so we exercise the
    agent-side loop without depending on aiomqtt. Both the publish call
    order and the broker PUBACK→outbox prune flow are asserted.
    """

    class _RecordingTransport:
        confirms_on_publish = True

        def __init__(self):
            self.calls: List[Tuple[str, dict]] = []

        async def connect(self):
            return None

        async def close(self):
            return None

        async def send_state(self, frame):
            self.calls.append(("lifecycle", frame))

        async def send_sample(self, frame):
            self.calls.append(("sample_batch", frame))

        async def send_alarm(self, frame):
            self.calls.append(("alarm_event", frame))

        async def publish(self, frame):
            t = frame.get("type")
            if t == "lifecycle":
                await self.send_state(frame)
            elif t == "sample_batch":
                await self.send_sample(frame)
            elif t == "alarm_event":
                await self.send_alarm(frame)

    @pytest.mark.asyncio
    async def test_uplink_loop_drains_outbox_in_seq_order(self, tmp_path):
        outbox = DurableOutbox(str(tmp_path / "outbox.db"))
        transport = self._RecordingTransport()

        # Pre-load three frames — one of each uplink type, in order.
        outbox.append(lambda seq: {"type": "lifecycle", "edge_id": "e",
                                    "monotonic_seq": seq,
                                    "event": "session.online",
                                    "ts": "2026-05-28T00:00:00Z"})
        outbox.append(lambda seq: {"type": "sample_batch", "edge_id": "e",
                                    "monotonic_seq": seq, "task_id": 1,
                                    "task_code": "t1", "samples": [],
                                    "window_start": "2026-05-28T00:00:00Z",
                                    "window_end": "2026-05-28T00:00:01Z"})
        outbox.append(lambda seq: {"type": "alarm_event", "edge_id": "e",
                                    "monotonic_seq": seq, "rule_id": 1,
                                    "point_code": "p", "device_code": "",
                                    "value": 1, "severity": "warning",
                                    "status": "firing", "message": "",
                                    "fired_at": "2026-05-28T00:00:01Z"})

        agent = EdgeAgent(_cfg(), durable_outbox=outbox,
                          uplink_transport=transport)
        # Signal so the loop doesn't sit waiting for the next append.
        agent._uplink_signal.set()

        task = asyncio.create_task(agent._uplink_loop(transport))
        # Wait until the outbox empties (broker-PUBACK prune drains rows).
        for _ in range(200):
            await asyncio.sleep(0.01)
            if outbox.depth() == 0 and len(transport.calls) == 3:
                break
        agent.stop()
        await asyncio.wait_for(task, timeout=1.0)

        types = [t for t, _ in transport.calls]
        assert types == ["lifecycle", "sample_batch", "alarm_event"]
        seqs = [f["monotonic_seq"] for _, f in transport.calls]
        assert seqs == [1, 2, 3]
        # PUBACK at QoS 1 = delivery → outbox row pruned.
        assert outbox.depth() == 0

    @pytest.mark.asyncio
    async def test_backfill_tag_applied_to_history(self, tmp_path):
        """Frames at/below ``_backfill_through`` ship with ``backfill: True``."""
        outbox = DurableOutbox(str(tmp_path / "outbox.db"))
        outbox.append(lambda seq: {"type": "lifecycle", "edge_id": "e",
                                    "monotonic_seq": seq,
                                    "event": "session.online",
                                    "ts": "2026-05-28T00:00:00Z"})
        outbox.append(lambda seq: {"type": "lifecycle", "edge_id": "e",
                                    "monotonic_seq": seq,
                                    "event": "session.online",
                                    "ts": "2026-05-28T00:00:00Z"})
        transport = self._RecordingTransport()
        agent = EdgeAgent(_cfg(), durable_outbox=outbox,
                          uplink_transport=transport)
        # Mark seq=1 as backfill (reconnect resume point).
        agent._backfill_through = 1
        agent._uplink_signal.set()
        task = asyncio.create_task(agent._uplink_loop(transport))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(transport.calls) == 2:
                break
        agent.stop()
        await asyncio.wait_for(task, timeout=1.0)

        first, second = transport.calls
        assert first[1].get("backfill") is True
        assert "backfill" not in second[1]


# ---------------------------------------------------------------------------
# Live-broker integration test (opt-in)
# ---------------------------------------------------------------------------
#
# Acceptance from the issue says: "启动本地 mosquitto + center mock
# subscriber → edge 发 lifecycle/sample/alarm 三种消息 → center 收到顺序、
# payload schema 正确". When ``EDGE_MQTT_TEST_BROKER=host[:port]`` is set
# the test below talks to a real broker the way the M3 smoke runbook does.
# It is skipped on every other run so day-to-day CI stays hermetic.


@pytest.mark.asyncio
async def test_live_broker_roundtrip():
    broker = os.environ.get("EDGE_MQTT_TEST_BROKER")
    if not broker:
        pytest.skip("set EDGE_MQTT_TEST_BROKER=host[:port] to run")
    aiomqtt = pytest.importorskip("aiomqtt")

    host, _, port = broker.partition(":")
    port_i = int(port) if port else 1883
    edge_id = "edge-mqtt-it"

    received: List[Tuple[str, dict]] = []
    ready = asyncio.Event()
    stop = asyncio.Event()

    async def _consumer():
        async with aiomqtt.Client(hostname=host, port=port_i,
                                  identifier=f"it-sub-{edge_id}") as client:
            await client.subscribe(f"edge/{edge_id}/uplink/#", qos=1)
            ready.set()
            async for msg in client.messages:
                received.append((str(msg.topic),
                                  json.loads(msg.payload.decode("utf-8"))))
                if len(received) >= 3:
                    stop.set()
                    return

    consumer = asyncio.create_task(_consumer())
    await asyncio.wait_for(ready.wait(), timeout=10)

    transport = MqttTransport(
        broker_url=f"mqtt://{host}:{port_i}", edge_id=edge_id,
        client_id=f"it-pub-{edge_id}",
    )
    await transport.connect()
    frames = [
        {"type": "lifecycle", "edge_id": edge_id, "monotonic_seq": 1,
         "event": "session.online", "ts": "2026-05-28T00:00:00Z"},
        {"type": "sample_batch", "edge_id": edge_id, "monotonic_seq": 2,
         "task_id": 1, "task_code": "t1",
         "window_start": "2026-05-28T00:00:00Z",
         "window_end": "2026-05-28T00:00:01Z",
         "samples": [{"point_code": "p", "value": 1.0, "quality": "good",
                       "timestamp": "2026-05-28T00:00:00.500Z"}]},
        {"type": "alarm_event", "edge_id": edge_id, "monotonic_seq": 3,
         "rule_id": 1, "point_code": "p", "device_code": "",
         "value": 1, "severity": "warning", "status": "firing",
         "message": "", "fired_at": "2026-05-28T00:00:01Z"},
    ]
    for f in frames:
        await transport.publish(f)

    try:
        await asyncio.wait_for(stop.wait(), timeout=10)
    finally:
        await transport.close()
        consumer.cancel()
        try:
            await consumer
        except (asyncio.CancelledError, Exception):
            pass

    topics = [t for t, _ in received]
    assert topics == [
        f"edge/{edge_id}/uplink/lifecycle",
        f"edge/{edge_id}/uplink/sample_batch",
        f"edge/{edge_id}/uplink/alarm_event",
    ]
    payloads = [p for _, p in received]
    assert [p["monotonic_seq"] for p in payloads] == [1, 2, 3]
    assert payloads[0]["event"] == "session.online"
    assert payloads[1]["samples"][0]["point_code"] == "p"
    assert payloads[2]["rule_id"] == 1
