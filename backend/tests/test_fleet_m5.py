"""Tests for the M5 offline-degradation + backfill layer of fleet/ (XIU-72).

Covers the center side of M5:
- the register ack reporting the high-water ``last_uplink_seq`` so a
  reconnecting edge knows where to start its durable-outbox backfill
- per-uplink-frame + heartbeat acks carrying ``last_uplink_seq`` so the
  edge can prune its outbox
- ``heartbeat.buffer`` mirrored onto ``EdgeNode.buffer_backlog``
- ``backfill``-tagged frames stamping ``EdgeNode.last_backfill_at``
- idempotent re-ingest of a backfilled duplicate seq (no double-record)
- the ``EdgeNodeSerializer`` exposing the M5 observability fields
"""
from __future__ import annotations

import pytest
from asgiref.sync import sync_to_async
from channels.layers import channel_layers
from channels.routing import URLRouter
from django.conf import settings
from channels.testing import WebsocketCommunicator

from fleet.models import EdgeLifecycleEvent, EdgeNode
from fleet.protocol import FRAME_ACK, PROTOCOL_VERSION, make_lifecycle, make_register
from fleet.routing import websocket_urlpatterns
from fleet.serializers import EdgeNodeSerializer

pytestmark = pytest.mark.django_db


fleet_application = URLRouter(websocket_urlpatterns)


@pytest.fixture(autouse=True)
def _patch_db_defaults():
    db = settings.DATABASES["default"]
    db.setdefault("TIME_ZONE", None)
    db.setdefault("CONN_HEALTH_CHECKS", False)
    db.setdefault("CONN_MAX_AGE", 0)
    db.setdefault("AUTOCOMMIT", True)
    db.setdefault("OPTIONS", {})
    yield


@pytest.fixture
def _inmemory_channel_layer():
    original = settings.CHANNEL_LAYERS
    settings.CHANNEL_LAYERS = {
        "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
    }
    channel_layers.backends = {}
    yield
    settings.CHANNEL_LAYERS = original
    channel_layers.backends = {}


async def _register(comm, edge_name: str, token: str, *, uplink_seq: int = 0) -> dict:
    frame = make_register(edge_id=edge_name, token=token, version="0.5.0")
    frame["uplink_seq"] = uplink_seq
    await comm.send_json_to(frame)
    return await comm.receive_json_from()


# ---------------------------------------------------------------------------
# register ack — backfill resume point
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_ack_carries_last_uplink_seq(_inmemory_channel_layer):
    """The register ack reports the center's high-water mark so the edge
    knows where to resume its backfill from."""
    node, token = await sync_to_async(EdgeNode.issue)(name="edge-m5-reg")
    node.last_uplink_seq = 42
    await sync_to_async(node.save)(update_fields=["last_uplink_seq"])

    comm = WebsocketCommunicator(fleet_application, "/ws/fleet/")
    await comm.connect()
    ack = await _register(comm, "edge-m5-reg", token)
    assert ack["type"] == FRAME_ACK
    assert ack["last_uplink_seq"] == 42
    await comm.disconnect()


@pytest.mark.asyncio
async def test_fresh_edge_register_ack_is_zero(_inmemory_channel_layer):
    """A brand-new edge has no high-water mark — the ack reports 0 so the
    edge backfills its whole outbox from seq 1."""
    node, token = await sync_to_async(EdgeNode.issue)(name="edge-m5-fresh")
    comm = WebsocketCommunicator(fleet_application, "/ws/fleet/")
    await comm.connect()
    ack = await _register(comm, "edge-m5-fresh", token)
    assert ack["last_uplink_seq"] == 0
    await comm.disconnect()


# ---------------------------------------------------------------------------
# heartbeat — buffer backlog mirror + ack seq
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_no_longer_mirrors_buffer_backlog(_inmemory_channel_layer):
    """Phase 2 P4 (XIU-103) retired the WS-heartbeat-driven DB writes.

    The pre-P4 handler folded ``heartbeat.buffer`` into
    ``EdgeNode.buffer_backlog``; presence + backlog telemetry have to
    come over MQTT now (status from the retained LWT, backlog from a
    follow-up P5 topic). The consumer still acks the heartbeat for
    rolling-upgrade compat, but no DB write happens. The pre-P4 body
    is preserved in :func:`fleet._legacy.legacy_handle_heartbeat`.
    """
    node, token = await sync_to_async(EdgeNode.issue)(name="edge-m5-hb")
    comm = WebsocketCommunicator(fleet_application, "/ws/fleet/")
    await comm.connect()
    await _register(comm, "edge-m5-hb", token)

    await comm.send_json_to({
        "v": PROTOCOL_VERSION, "type": "heartbeat", "edge_id": "edge-m5-hb",
        "uptime": 12.0, "tasks": 1, "buffer": 137,
    })
    ack = await comm.receive_json_from()
    assert ack["type"] == FRAME_ACK
    assert "last_uplink_seq" in ack  # seq ridealong still served

    await sync_to_async(node.refresh_from_db)()
    # The heartbeat no longer touches ``buffer_backlog`` — it stays at
    # whatever the previous source updated it to (0 on a fresh edge).
    assert node.buffer_backlog == 0
    await comm.disconnect()


@pytest.mark.asyncio
async def test_heartbeat_without_buffer_field_keeps_zero(_inmemory_channel_layer):
    """A heartbeat without ``buffer`` is acked cleanly — backlog stays 0."""
    node, token = await sync_to_async(EdgeNode.issue)(name="edge-m5-hb0")
    comm = WebsocketCommunicator(fleet_application, "/ws/fleet/")
    await comm.connect()
    await _register(comm, "edge-m5-hb0", token)
    await comm.send_json_to({
        "v": PROTOCOL_VERSION, "type": "heartbeat", "edge_id": "edge-m5-hb0",
    })
    await comm.receive_json_from()
    await sync_to_async(node.refresh_from_db)()
    assert node.buffer_backlog == 0
    await comm.disconnect()


# ---------------------------------------------------------------------------
# backfill tagging — last_backfill_at stamp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfilled_frame_stamps_last_backfill_at(_inmemory_channel_layer):
    """A frame the edge replays from its outbox carries ``backfill: true``;
    the center stamps ``EdgeNode.last_backfill_at``."""
    node, token = await sync_to_async(EdgeNode.issue)(name="edge-m5-bf")
    comm = WebsocketCommunicator(fleet_application, "/ws/fleet/")
    await comm.connect()
    await _register(comm, "edge-m5-bf", token)

    frame = make_lifecycle(
        edge_id="edge-m5-bf", monotonic_seq=1, event="session.online")
    frame["backfill"] = True
    await comm.send_json_to(frame)
    await comm.receive_json_from()

    await sync_to_async(node.refresh_from_db)()
    assert node.last_backfill_at is not None
    await comm.disconnect()


@pytest.mark.asyncio
async def test_live_frame_does_not_stamp_last_backfill_at(_inmemory_channel_layer):
    """A normal (un-tagged) live frame must not stamp the backfill time."""
    node, token = await sync_to_async(EdgeNode.issue)(name="edge-m5-live")
    comm = WebsocketCommunicator(fleet_application, "/ws/fleet/")
    await comm.connect()
    await _register(comm, "edge-m5-live", token)

    await comm.send_json_to(make_lifecycle(
        edge_id="edge-m5-live", monotonic_seq=1, event="session.online"))
    await comm.receive_json_from()

    await sync_to_async(node.refresh_from_db)()
    assert node.last_backfill_at is None
    await comm.disconnect()


# ---------------------------------------------------------------------------
# per-frame ack seq + idempotent backfill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uplink_ack_carries_seq(_inmemory_channel_layer):
    """Every uplink-frame ack carries ``last_uplink_seq`` so the edge can
    prune its durable outbox up to the confirmed seq."""
    node, token = await sync_to_async(EdgeNode.issue)(name="edge-m5-ack")
    comm = WebsocketCommunicator(fleet_application, "/ws/fleet/")
    await comm.connect()
    await _register(comm, "edge-m5-ack", token)

    await comm.send_json_to(make_lifecycle(
        edge_id="edge-m5-ack", monotonic_seq=1, event="session.online"))
    ack = await comm.receive_json_from()
    assert ack["last_uplink_seq"] == 1
    await comm.disconnect()


@pytest.mark.asyncio
async def test_backfill_duplicate_is_idempotent(_inmemory_channel_layer):
    """Re-ingesting a seq the center already has (a redelivered backfill
    frame) is dropped as a duplicate — no double-record, seq unchanged."""
    node, token = await sync_to_async(EdgeNode.issue)(name="edge-m5-dup")
    node.last_uplink_seq = 5
    await sync_to_async(node.save)(update_fields=["last_uplink_seq"])

    comm = WebsocketCommunicator(fleet_application, "/ws/fleet/")
    await comm.connect()
    await _register(comm, "edge-m5-dup", token)

    # seq 3 <= high-water 5 → duplicate, dropped.
    frame = make_lifecycle(
        edge_id="edge-m5-dup", monotonic_seq=3, event="session.online")
    frame["backfill"] = True
    await comm.send_json_to(frame)
    ack = await comm.receive_json_from()
    # The ack still reports the (unmoved) high-water mark.
    assert ack["last_uplink_seq"] == 5

    await sync_to_async(node.refresh_from_db)()
    assert node.last_uplink_seq == 5
    rows = await sync_to_async(
        EdgeLifecycleEvent.objects.filter(edge=node).count
    )()
    assert rows == 0  # duplicate never recorded
    await comm.disconnect()


# ---------------------------------------------------------------------------
# serializer — /fleet observability fields
# ---------------------------------------------------------------------------


def test_edgenode_serializer_exposes_m5_fields():
    """The /fleet projection surfaces the M5 backlog / backfill fields."""
    node, _ = EdgeNode.issue(name="edge-m5-ser")
    node.last_uplink_seq = 12
    node.buffer_backlog = 8
    node.save(update_fields=["last_uplink_seq", "buffer_backlog"])

    data = EdgeNodeSerializer(node).data
    assert data["last_uplink_seq"] == 12
    assert data["buffer_backlog"] == 8
    assert "last_backfill_at" in data
