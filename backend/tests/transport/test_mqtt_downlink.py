"""MQTT downlink (center → edge command) tests — XIU-101 Phase 2 P2.

``publish_command()`` is sync-callable on top of paho-mqtt; the
``dispatch_apply_config`` dispatcher in ``fleet.services`` routes by
``settings.FLEET_TRANSPORT`` (``mqtt|ws|both``). These tests inject a
fake publisher and a fake Channels layer so we never depend on a live
broker / Redis and can assert directly on the wire-shape: topic, QoS,
JSON payload, and the v0.5 ``apply_config`` schema fields.

A live-broker integration smoke is included at the bottom — skipped
unless ``FLEET_MQTT_TEST_BROKER`` is set, matching the convention used
in ``test_mqtt_subscribe.py`` (P1).
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
from dataclasses import dataclass, field
from typing import Any, List, Tuple

import pytest
from asgiref.sync import sync_to_async
from channels.layers import channel_layers
from django.conf import settings

from configuration.models import AcqTask, Device, Point, PointTemplate, Site
from fleet import mqtt_transport
from fleet.models import EdgeNode
from fleet.mqtt_transport import CMD_TOPIC_TEMPLATE, publish_command
from fleet.protocol import PROTOCOL_VERSION
from fleet.services import (
    FLEET_TRANSPORT_BOTH,
    FLEET_TRANSPORT_MQTT,
    FLEET_TRANSPORT_WS,
    build_apply_config_payload,
    dispatch_apply_config,
    reconcile_assignments,
    sync_assignments,
)


pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakePublisher:
    """Stand-in for :class:`fleet.mqtt_transport._PublisherManager`.

    Records every ``publish()`` call so tests can assert topic/QoS/payload
    without a broker. ``accept`` toggles the return code so we can test
    the "broker rejected" path.
    """

    accept: bool = True
    calls: List[Tuple[str, bytes, int]] = field(default_factory=list)

    def publish(self, topic: str, payload: bytes, qos: int = 1) -> bool:
        self.calls.append((topic, payload, qos))
        return self.accept


@dataclass
class _RecordingChannelLayer:
    """In-memory stand-in for the channel layer used by the WS dispatch."""

    sends: List[Tuple[str, dict]] = field(default_factory=list)

    async def group_send(self, group: str, message: dict) -> None:
        self.sends.append((group, message))


@pytest.fixture(autouse=True)
def _patch_db_defaults():
    """Mirror test_fleet_m2.py — re-inject Django 4.2 connection defaults the
    session-scoped conftest fixture drops, so database_sync_to_async / sync
    helpers used here keep working."""
    db = settings.DATABASES["default"]
    db.setdefault("TIME_ZONE", None)
    db.setdefault("CONN_HEALTH_CHECKS", False)
    db.setdefault("CONN_MAX_AGE", 0)
    db.setdefault("AUTOCOMMIT", True)
    db.setdefault("OPTIONS", {})
    yield


@pytest.fixture
def _inmemory_channel_layer():
    """Force the in-memory channel layer so the WS dispatch path is testable."""
    original = settings.CHANNEL_LAYERS
    settings.CHANNEL_LAYERS = {
        "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
    }
    channel_layers.backends = {}
    yield
    settings.CHANNEL_LAYERS = original
    channel_layers.backends = {}


@pytest.fixture
def fake_publisher(monkeypatch):
    """Replace the module-level default publisher with a recording fake."""
    pub = _FakePublisher()
    monkeypatch.setattr(mqtt_transport, "default_publisher", pub)
    return pub


@pytest.fixture
def transport_mode(monkeypatch, request):
    """Parametrisable FLEET_TRANSPORT for one test."""
    monkeypatch.setattr(settings, "FLEET_TRANSPORT", request.param, raising=False)
    return request.param


# ---------------------------------------------------------------------------
# publish_command — unit
# ---------------------------------------------------------------------------


def test_publish_command_topic_qos_and_json_encoding():
    """edge/<edge>/cmd/<type>, QoS 1 default, JSON-encoded payload."""
    pub = _FakePublisher()
    ok = publish_command(
        "test-edge",
        "apply_config",
        {"v": PROTOCOL_VERSION, "type": "apply_config"},
        publisher=pub,
    )
    assert ok is True
    assert len(pub.calls) == 1
    topic, payload, qos = pub.calls[0]
    assert topic == "edge/test-edge/cmd/apply_config"
    assert topic == CMD_TOPIC_TEMPLATE.format(edge_id="test-edge", cmd_type="apply_config")
    assert qos == 1
    assert json.loads(payload.decode("utf-8")) == {
        "v": PROTOCOL_VERSION, "type": "apply_config",
    }


def test_publish_command_explicit_qos_passed_through():
    pub = _FakePublisher()
    publish_command("e1", "restart_task", {"task_id": 7}, qos=0, publisher=pub)
    assert pub.calls[0][2] == 0


def test_publish_command_returns_false_when_publisher_rejects():
    pub = _FakePublisher(accept=False)
    assert publish_command("e1", "x", {"a": 1}, publisher=pub) is False


def test_publish_command_drops_non_json_payload_without_crashing():
    pub = _FakePublisher()
    # `set` is not JSON-serialisable — must be logged + returned False, not
    # raised, so a bad caller can't 500 a DRF view.
    assert publish_command("e1", "x", {1, 2, 3}, publisher=pub) is False
    assert pub.calls == []


# ---------------------------------------------------------------------------
# dispatch_apply_config — transport switch
# ---------------------------------------------------------------------------


@pytest.fixture
def _edge_with_one_task(_inmemory_channel_layer):
    """Real EdgeNode + minimal AcqTask so build_apply_config_payload returns
    a non-trivial v0.5 frame (tasks/devices/points populated)."""
    edge, _token = EdgeNode.issue(name="test-edge")
    site, _ = Site.objects.get_or_create(code="default", defaults={"name": "default"})
    device = Device.objects.create(
        site=site, code="dev-a", name="Device A",
        protocol="modbus_tcp", ip_address="127.0.0.1", port=5020, metadata={},
    )
    tpl = PointTemplate.objects.create(
        name="tpl-a", english_name="tpl-a", unit="", data_type="uint16",
    )
    task = AcqTask.objects.create(code="task-a", name="Task A", edge=edge)
    point = Point.objects.create(device=device, template=tpl, code="task-a-p0", address="40001")
    task.points.add(point)
    return edge


def _v05_apply_config_payload_for(edge: EdgeNode) -> dict:
    version, _assignments = reconcile_assignments(edge)
    return build_apply_config_payload(edge, version=version)


def _assert_v05_apply_config_schema(frame: dict) -> None:
    """Sanity-check that a wire frame matches the v0.5 apply_config shape."""
    assert frame["v"] == PROTOCOL_VERSION == "0.5"
    assert frame["type"] == "apply_config"
    for key in ("version", "tasks", "devices", "points", "alarm_rules"):
        assert key in frame, f"v0.5 apply_config missing {key!r}"
    assert isinstance(frame["version"], int)
    assert isinstance(frame["tasks"], list)
    assert isinstance(frame["devices"], list)
    assert isinstance(frame["points"], list)
    assert isinstance(frame["alarm_rules"], list)


def test_dispatch_mqtt_only_publishes_via_mqtt_not_ws(
    monkeypatch, fake_publisher, _edge_with_one_task,
):
    """FLEET_TRANSPORT=mqtt → exactly one MQTT publish, zero WS group_sends."""
    monkeypatch.setattr(settings, "FLEET_TRANSPORT", FLEET_TRANSPORT_MQTT, raising=False)

    layer = _RecordingChannelLayer()
    monkeypatch.setattr("fleet.services.get_channel_layer", lambda: layer)

    edge = _edge_with_one_task
    frame = _v05_apply_config_payload_for(edge)

    assert dispatch_apply_config(edge, frame) is True

    assert layer.sends == [], "WS path must not be touched in mqtt-only mode"
    assert len(fake_publisher.calls) == 1
    topic, payload, qos = fake_publisher.calls[0]
    assert topic == f"edge/{edge.name}/cmd/apply_config"
    assert qos == 1
    decoded = json.loads(payload.decode("utf-8"))
    _assert_v05_apply_config_schema(decoded)
    # Round-trip is identity (the frame ``services`` built equals what arrives
    # on the wire) — the assertion the issue calls "payload schema 同协议 v0.5".
    assert decoded == frame


def test_dispatch_ws_only_uses_channel_layer_not_mqtt(
    monkeypatch, fake_publisher, _edge_with_one_task,
):
    """FLEET_TRANSPORT=ws → exactly one WS group_send, zero MQTT publishes."""
    monkeypatch.setattr(settings, "FLEET_TRANSPORT", FLEET_TRANSPORT_WS, raising=False)

    layer = _RecordingChannelLayer()
    monkeypatch.setattr("fleet.services.get_channel_layer", lambda: layer)

    edge = _edge_with_one_task
    frame = _v05_apply_config_payload_for(edge)

    assert dispatch_apply_config(edge, frame) is True

    assert fake_publisher.calls == [], "MQTT publisher must not be touched in ws-only mode"
    assert len(layer.sends) == 1
    group, message = layer.sends[0]
    assert group == f"fleet.edge.{edge.pk}"
    assert message["type"] == "fleet.send"
    assert message["frame"] == frame


def test_dispatch_both_sends_to_each_transport_exactly_once(
    monkeypatch, fake_publisher, _edge_with_one_task,
):
    """FLEET_TRANSPORT=both — verifies 验收 "既能下发又不重复"."""
    monkeypatch.setattr(settings, "FLEET_TRANSPORT", FLEET_TRANSPORT_BOTH, raising=False)

    layer = _RecordingChannelLayer()
    monkeypatch.setattr("fleet.services.get_channel_layer", lambda: layer)

    edge = _edge_with_one_task
    frame = _v05_apply_config_payload_for(edge)

    assert dispatch_apply_config(edge, frame) is True

    # Exactly one per transport — no duplicate WS or MQTT publish per call.
    assert len(layer.sends) == 1
    assert len(fake_publisher.calls) == 1

    # Both carry the same frame (no schema drift between transports).
    ws_group, ws_msg = layer.sends[0]
    assert ws_group == f"fleet.edge.{edge.pk}"
    assert ws_msg["frame"] == frame

    topic, payload, qos = fake_publisher.calls[0]
    assert topic == f"edge/{edge.name}/cmd/apply_config"
    assert qos == 1
    assert json.loads(payload.decode("utf-8")) == frame


def test_dispatch_unknown_transport_value_falls_back_to_both(
    monkeypatch, fake_publisher, _edge_with_one_task,
):
    """A typo / misconfiguration shouldn't silently drop downlink frames."""
    monkeypatch.setattr(settings, "FLEET_TRANSPORT", "garbage", raising=False)

    layer = _RecordingChannelLayer()
    monkeypatch.setattr("fleet.services.get_channel_layer", lambda: layer)

    edge = _edge_with_one_task
    frame = _v05_apply_config_payload_for(edge)
    assert dispatch_apply_config(edge, frame) is True

    assert len(layer.sends) == 1
    assert len(fake_publisher.calls) == 1


def test_dispatch_returns_false_when_no_transport_succeeds(
    monkeypatch, _edge_with_one_task,
):
    """Both transports failing → caller sees ``False`` so it can react."""
    monkeypatch.setattr(settings, "FLEET_TRANSPORT", FLEET_TRANSPORT_BOTH, raising=False)
    monkeypatch.setattr("fleet.services.get_channel_layer", lambda: None)
    monkeypatch.setattr(mqtt_transport, "default_publisher", _FakePublisher(accept=False))

    edge = _edge_with_one_task
    frame = _v05_apply_config_payload_for(edge)
    assert dispatch_apply_config(edge, frame) is False


def test_sync_assignments_uses_configured_transport(
    monkeypatch, fake_publisher, _edge_with_one_task,
):
    """The top-level sync helper threads through the same transport flag.

    Verifies that ``sync_alarm_rules_to_edges`` (which goes via
    ``sync_assignments`` → ``dispatch_apply_config``) inherits the
    transport switch without separate plumbing.
    """
    monkeypatch.setattr(settings, "FLEET_TRANSPORT", FLEET_TRANSPORT_MQTT, raising=False)
    layer = _RecordingChannelLayer()
    monkeypatch.setattr("fleet.services.get_channel_layer", lambda: layer)

    edge = _edge_with_one_task
    summary = sync_assignments(edge)

    assert summary["delivered"] is True
    assert layer.sends == []
    assert len(fake_publisher.calls) == 1
    topic, payload, _qos = fake_publisher.calls[0]
    assert topic == f"edge/{edge.name}/cmd/apply_config"
    on_wire = json.loads(payload.decode("utf-8"))
    _assert_v05_apply_config_schema(on_wire)
    assert on_wire["version"] == summary["config_version"]


# ---------------------------------------------------------------------------
# Optional live-broker smoke — opt-in via FLEET_MQTT_TEST_BROKER, mirrors the
# P1 subscriber smoke so the M2 runbook can flip ``FLEET_TRANSPORT=mqtt``
# and confirm round-trip with ``mosquitto_sub``.
# ---------------------------------------------------------------------------


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
async def test_live_broker_publish_command_round_trip():
    """End-to-end: publish_command → real broker → aiomqtt subscriber receives."""
    broker = _live_broker()
    if broker is None:
        pytest.skip(
            "FLEET_MQTT_TEST_BROKER not set or broker unreachable — "
            "run `docker compose -f docker-compose.center.yml up -d mosquitto` "
            "and re-run with FLEET_MQTT_TEST_BROKER=127.0.0.1:1883"
        )
    aiomqtt = pytest.importorskip("aiomqtt")
    host, port = broker

    # Point the publisher singleton at the live broker, then reset so the
    # next test gets a clean default. Sync settings reach the manager via
    # _config_from_settings at first publish.
    from django.conf import settings as live_settings

    live_settings.FLEET_MQTT_HOST = host
    live_settings.FLEET_MQTT_PORT = port
    live_settings.FLEET_MQTT_PUBLISHER_CLIENT_ID = f"test-pub-{os.getpid()}"
    mqtt_transport.default_publisher.stop()

    received: asyncio.Queue = asyncio.Queue()

    async def subscriber():
        async with aiomqtt.Client(
            hostname=host, port=port, identifier=f"test-sub-{os.getpid()}"
        ) as client:
            await client.subscribe("edge/test-edge/cmd/+", qos=1)
            async for message in client.messages:
                await received.put((str(message.topic), bytes(message.payload)))

    sub_task = asyncio.create_task(subscriber())
    try:
        # Give the subscriber's CONNECT/SUBSCRIBE roundtrip a beat to land.
        await asyncio.sleep(0.5)
        frame = {
            "v": PROTOCOL_VERSION,
            "type": "apply_config",
            "version": 1,
            "tasks": [],
            "devices": [],
            "points": [],
            "alarm_rules": [],
        }
        # ``publish_command`` is sync; run on the default executor so the
        # asyncio loop keeps draining the subscriber queue.
        ok = await asyncio.get_running_loop().run_in_executor(
            None, lambda: publish_command("test-edge", "apply_config", frame, qos=1)
        )
        assert ok is True

        topic, payload = await asyncio.wait_for(received.get(), timeout=3.0)
        assert topic == "edge/test-edge/cmd/apply_config"
        assert json.loads(payload.decode("utf-8")) == frame
    finally:
        sub_task.cancel()
        try:
            await sub_task
        except asyncio.CancelledError:
            pass
        mqtt_transport.default_publisher.stop()
