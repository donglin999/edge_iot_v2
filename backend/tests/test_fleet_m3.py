"""Tests for the M3 uplink layer of the fleet/ app (XIU-63).

Covers:
- ``classify_uplink_seq`` — duplicate / gap / advance / restart classification
- ``make_lifecycle`` / ``make_sample_batch`` frame builders
- WS ingest of ``lifecycle`` frames → EdgeTaskStatus + EdgeLifecycleEvent
- WS ingest of ``sample_batch`` frames → EdgeSample aggregation cache
- per-edge ``last_uplink_seq`` advancement + duplicate drop
- center-synthesised ``session.offline`` on disconnect
- the browser-facing FleetTaskStatusConsumer (snapshot + live push)
- REST projections of the new tables
"""
from __future__ import annotations

import pytest
from asgiref.sync import sync_to_async
from channels.layers import channel_layers
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.conf import settings
from rest_framework.test import APIClient

from configuration.models import AcqTask, Device, Point, PointTemplate, Site
from fleet.models import (
    EdgeLifecycleEvent,
    EdgeNode,
    EdgeSample,
    EdgeTaskStatus,
    UPLINK_SEQ_ADVANCED,
    UPLINK_SEQ_DUPLICATE,
    UPLINK_SEQ_GAP,
    classify_uplink_seq,
)
from fleet.protocol import (
    FRAME_ACK,
    FRAME_ERROR,
    LIFECYCLE_SESSION_OFFLINE,
    PROTOCOL_VERSION,
    make_lifecycle,
    make_register,
    make_sample_batch,
    make_task_state,
    task_state_to_lifecycle_event,
)
from fleet.routing import websocket_urlpatterns

pytestmark = pytest.mark.django_db


fleet_application = URLRouter(websocket_urlpatterns)


@pytest.fixture(autouse=True)
def _patch_db_defaults():
    """Re-inject Django 4.2 connection defaults the session conftest drops."""
    db = settings.DATABASES["default"]
    db.setdefault("TIME_ZONE", None)
    db.setdefault("CONN_HEALTH_CHECKS", False)
    db.setdefault("CONN_MAX_AGE", 0)
    db.setdefault("AUTOCOMMIT", True)
    db.setdefault("OPTIONS", {})
    yield


@pytest.fixture
def _inmemory_channel_layer():
    """Swap the Redis channel layer for an in-memory one for WS tests."""
    original = settings.CHANNEL_LAYERS
    settings.CHANNEL_LAYERS = {
        "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
    }
    channel_layers.backends = {}
    yield
    settings.CHANNEL_LAYERS = original
    channel_layers.backends = {}


@pytest.fixture(autouse=True)
def _no_influx_mirror():
    """Disable the best-effort InfluxDB sample mirror so tests stay offline."""
    original = getattr(settings, "CENTER_EDGE_SAMPLE_TO_INFLUX", True)
    settings.CENTER_EDGE_SAMPLE_TO_INFLUX = False
    yield
    settings.CENTER_EDGE_SAMPLE_TO_INFLUX = original


def _make_task(*, code: str, edge: EdgeNode | None) -> AcqTask:
    site, _ = Site.objects.get_or_create(code="default", defaults={"name": "default"})
    device = Device.objects.create(
        site=site, code=f"dev-{code}", name=f"Device {code}",
        protocol="modbus_tcp", ip_address="127.0.0.1", port=5020, metadata={},
    )
    tpl = PointTemplate.objects.create(
        name=f"tpl-{code}", english_name=f"tpl-{code}", unit="", data_type="uint16"
    )
    task = AcqTask.objects.create(code=code, name=f"Task {code}", edge=edge)
    for i in range(2):
        point = Point.objects.create(
            device=device, template=tpl, code=f"{code}-p{i}", address=str(40001 + i)
        )
        task.points.add(point)
    return task


async def _register(comm, edge_name: str, token: str) -> dict:
    await comm.send_json_to(make_register(edge_id=edge_name, token=token, version="0.3.0"))
    return await comm.receive_json_from()


# ---------------------------------------------------------------------------
# classify_uplink_seq — pure logic
# ---------------------------------------------------------------------------


class TestClassifyUplinkSeq:
    def test_advance_by_one(self):
        assert classify_uplink_seq(4, 5) == UPLINK_SEQ_ADVANCED

    def test_gap(self):
        assert classify_uplink_seq(4, 9) == UPLINK_SEQ_GAP

    def test_duplicate(self):
        assert classify_uplink_seq(10, 10) == UPLINK_SEQ_DUPLICATE
        assert classify_uplink_seq(10, 8) == UPLINK_SEQ_DUPLICATE

    def test_first_frame_after_register_always_advances(self):
        # prev == 0 means the center just reset on register: whatever seq
        # the edge is at, this is the start of the new stream — never a
        # gap, never a duplicate. Covers a cold start (seq 1)...
        assert classify_uplink_seq(0, 1) == UPLINK_SEQ_ADVANCED
        # ...and a reconnect of a long-running agent (per-process seq N).
        assert classify_uplink_seq(0, 4731) == UPLINK_SEQ_ADVANCED

    def test_short_session_restart_is_not_a_duplicate(self):
        # XIU-63 review MAJOR: an agent that emitted 40 frames, restarted,
        # and re-registered must NOT have its new stream's seq 1..40 judged
        # stale. The register reset zeroes prev, so seq 1 is `advanced`.
        assert classify_uplink_seq(0, 1) == UPLINK_SEQ_ADVANCED


# ---------------------------------------------------------------------------
# frame builders
# ---------------------------------------------------------------------------


class TestFrameBuilders:
    def test_make_lifecycle_task_event(self):
        frame = make_lifecycle(
            edge_id="e1", monotonic_seq=3, event="task.running",
            task_id=7, task_code="t7",
        )
        assert frame["v"] == PROTOCOL_VERSION
        assert frame["type"] == "lifecycle"
        assert frame["monotonic_seq"] == 3
        assert frame["task_id"] == 7 and frame["task_code"] == "t7"
        assert "ts" in frame

    def test_make_lifecycle_rejects_bad_event(self):
        with pytest.raises(ValueError):
            make_lifecycle(edge_id="e1", monotonic_seq=1, event="task.exploded")

    def test_make_lifecycle_task_event_requires_task_id(self):
        with pytest.raises(ValueError):
            make_lifecycle(edge_id="e1", monotonic_seq=1, event="task.running")

    def test_task_state_maps_to_lifecycle_event(self):
        assert task_state_to_lifecycle_event("running") == "task.running"
        assert task_state_to_lifecycle_event("error") == "task.error"

    def test_make_sample_batch_shape(self):
        frame = make_sample_batch(
            edge_id="e1", monotonic_seq=9, task_id=2, task_code="t2",
            samples=[{"point_code": "p0", "value": 1.5, "quality": "good",
                      "timestamp": "2026-05-22T03:00:00Z"}],
            window_start="2026-05-22T02:59:59Z", window_end="2026-05-22T03:00:00Z",
        )
        assert frame["type"] == "sample_batch"
        assert frame["monotonic_seq"] == 9
        assert len(frame["samples"]) == 1


# ---------------------------------------------------------------------------
# WS ingest — lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifecycle_session_online_advances_seq(_inmemory_channel_layer):
    node, token = await sync_to_async(EdgeNode.issue)(name="edge-m3-online")

    comm = WebsocketCommunicator(fleet_application, "/ws/fleet/")
    connected, _ = await comm.connect()
    assert connected
    await _register(comm, "edge-m3-online", token)

    await comm.send_json_to(make_lifecycle(
        edge_id="edge-m3-online", monotonic_seq=1, event="session.online"
    ))
    ack = await comm.receive_json_from()
    assert ack["type"] == FRAME_ACK and ack["ref"] == "lifecycle"

    await sync_to_async(node.refresh_from_db)()
    assert node.last_uplink_seq == 1
    count = await sync_to_async(
        EdgeLifecycleEvent.objects.filter(edge=node, event="session.online").count
    )()
    assert count == 1

    await comm.disconnect()


@pytest.mark.asyncio
async def test_lifecycle_task_event_folds_into_task_status(_inmemory_channel_layer):
    node, token = await sync_to_async(EdgeNode.issue)(name="edge-m3-task")
    task = await sync_to_async(_make_task)(code="m3-task", edge=node)

    comm = WebsocketCommunicator(fleet_application, "/ws/fleet/")
    await comm.connect()
    await _register(comm, "edge-m3-task", token)

    await comm.send_json_to(make_lifecycle(
        edge_id="edge-m3-task", monotonic_seq=1, event="task.running",
        task_id=task.id, task_code=task.code,
    ))
    ack = await comm.receive_json_from()
    assert ack["ref"] == "lifecycle"

    status = await sync_to_async(EdgeTaskStatus.objects.get)(edge=node, task=task)
    assert status.state == "running"
    await sync_to_async(node.refresh_from_db)()
    assert node.last_uplink_seq == 1

    await comm.disconnect()


@pytest.mark.asyncio
async def test_duplicate_lifecycle_seq_is_dropped(_inmemory_channel_layer):
    node, token = await sync_to_async(EdgeNode.issue)(name="edge-m3-dup")
    task = await sync_to_async(_make_task)(code="m3-dup", edge=node)

    comm = WebsocketCommunicator(fleet_application, "/ws/fleet/")
    await comm.connect()
    await _register(comm, "edge-m3-dup", token)

    for seq, event in ((1, "task.starting"), (2, "task.running")):
        await comm.send_json_to(make_lifecycle(
            edge_id="edge-m3-dup", monotonic_seq=seq, event=event,
            task_id=task.id, task_code=task.code,
        ))
        await comm.receive_json_from()

    # Replay seq=1 — must be dropped: no new event row, seq stays at 2.
    await comm.send_json_to(make_lifecycle(
        edge_id="edge-m3-dup", monotonic_seq=1, event="task.starting",
        task_id=task.id, task_code=task.code,
    ))
    ack = await comm.receive_json_from()
    assert ack["type"] == FRAME_ACK

    await sync_to_async(node.refresh_from_db)()
    assert node.last_uplink_seq == 2
    events = await sync_to_async(
        EdgeLifecycleEvent.objects.filter(edge=node).count
    )()
    assert events == 2  # the replay did not add a third row

    await comm.disconnect()


# ---------------------------------------------------------------------------
# WS ingest — sample_batch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sample_batch_lands_in_aggregation_cache(_inmemory_channel_layer):
    node, token = await sync_to_async(EdgeNode.issue)(name="edge-m3-samp")
    task = await sync_to_async(_make_task)(code="m3-samp", edge=node)

    comm = WebsocketCommunicator(fleet_application, "/ws/fleet/")
    await comm.connect()
    await _register(comm, "edge-m3-samp", token)

    await comm.send_json_to(make_sample_batch(
        edge_id="edge-m3-samp", monotonic_seq=1, task_id=task.id, task_code=task.code,
        samples=[
            {"point_code": "p0", "value": 12.5, "quality": "good",
             "timestamp": "2026-05-22T03:00:00Z"},
            {"point_code": "p1", "value": 7, "quality": "good",
             "timestamp": "2026-05-22T03:00:00Z"},
        ],
        window_start="2026-05-22T02:59:59Z", window_end="2026-05-22T03:00:00Z",
    ))
    ack = await comm.receive_json_from()
    assert ack["type"] == FRAME_ACK and ack["ref"] == "sample_batch"

    samples = await sync_to_async(
        lambda: list(EdgeSample.objects.filter(edge=node).order_by("point_code"))
    )()
    assert [s.point_code for s in samples] == ["p0", "p1"]
    assert samples[0].value == 12.5
    assert samples[0].monotonic_seq == 1

    await sync_to_async(node.refresh_from_db)()
    assert node.last_uplink_seq == 1

    await comm.disconnect()


@pytest.mark.asyncio
async def test_sample_batch_updates_existing_row_in_place(_inmemory_channel_layer):
    node, token = await sync_to_async(EdgeNode.issue)(name="edge-m3-samp2")
    task = await sync_to_async(_make_task)(code="m3-samp2", edge=node)

    comm = WebsocketCommunicator(fleet_application, "/ws/fleet/")
    await comm.connect()
    await _register(comm, "edge-m3-samp2", token)

    for seq, value in ((1, 10.0), (2, 20.0)):
        await comm.send_json_to(make_sample_batch(
            edge_id="edge-m3-samp2", monotonic_seq=seq, task_id=task.id,
            task_code=task.code,
            samples=[{"point_code": "p0", "value": value, "quality": "good",
                      "timestamp": "2026-05-22T03:00:00Z"}],
            window_start="2026-05-22T02:59:59Z", window_end="2026-05-22T03:00:00Z",
        ))
        await comm.receive_json_from()

    rows = await sync_to_async(
        lambda: list(EdgeSample.objects.filter(edge=node, point_code="p0"))
    )()
    assert len(rows) == 1  # update-in-place, not append
    assert rows[0].value == 20.0

    await comm.disconnect()


# ---------------------------------------------------------------------------
# session.offline synthesis + browser task-status feed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_offline_synthesised_on_disconnect(_inmemory_channel_layer):
    node, token = await sync_to_async(EdgeNode.issue)(name="edge-m3-off")

    comm = WebsocketCommunicator(fleet_application, "/ws/fleet/")
    await comm.connect()
    await _register(comm, "edge-m3-off", token)
    await comm.disconnect()

    count = await sync_to_async(
        EdgeLifecycleEvent.objects.filter(
            edge=node, event=LIFECYCLE_SESSION_OFFLINE
        ).count
    )()
    assert count == 1


@pytest.mark.asyncio
async def test_task_status_consumer_pushes_live_change(_inmemory_channel_layer):
    node, token = await sync_to_async(EdgeNode.issue)(name="edge-m3-push")
    task = await sync_to_async(_make_task)(code="m3-push", edge=node)

    # Browser client subscribes to the live task-status feed.
    browser = WebsocketCommunicator(fleet_application, "/ws/fleet/task-statuses/")
    connected, _ = await browser.connect()
    assert connected
    snapshot = await browser.receive_json_from()
    assert snapshot["type"] == "snapshot"

    # Edge reports a task state → center should push it to the browser.
    edge = WebsocketCommunicator(fleet_application, "/ws/fleet/")
    await edge.connect()
    await _register(edge, "edge-m3-push", token)
    await edge.send_json_to(make_lifecycle(
        edge_id="edge-m3-push", monotonic_seq=1, event="task.running",
        task_id=task.id, task_code=task.code,
    ))
    await edge.receive_json_from()  # lifecycle ack

    pushed = await browser.receive_json_from()
    assert pushed["type"] == "task_status"
    assert pushed["data"]["task"] == task.id
    assert pushed["data"]["state"] == "running"

    await edge.disconnect()
    await browser.disconnect()


# ---------------------------------------------------------------------------
# XIU-63 review fixes — short-session restart + v0.2 push + seq validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_resets_last_uplink_seq(_inmemory_channel_layer):
    """Every register zeroes the high-water mark — a new socket is a new
    uplink stream."""
    node, token = await sync_to_async(EdgeNode.issue)(name="edge-m3-reset")
    node.last_uplink_seq = 99
    await sync_to_async(node.save)(update_fields=["last_uplink_seq"])

    comm = WebsocketCommunicator(fleet_application, "/ws/fleet/")
    await comm.connect()
    await _register(comm, "edge-m3-reset", token)

    await sync_to_async(node.refresh_from_db)()
    assert node.last_uplink_seq == 0
    await comm.disconnect()


@pytest.mark.asyncio
async def test_short_session_restart_keeps_new_stream(_inmemory_channel_layer):
    """XIU-63 review MAJOR regression: an agent that emitted a few frames,
    restarted, and re-registered must have its new stream's opening frames
    recorded — not dropped as stale duplicates."""
    node, token = await sync_to_async(EdgeNode.issue)(name="edge-m3-restart")
    task = await sync_to_async(_make_task)(code="m3-restart", edge=node)

    # --- session 1: emit 2 frames, then disconnect ---
    comm1 = WebsocketCommunicator(fleet_application, "/ws/fleet/")
    await comm1.connect()
    await _register(comm1, "edge-m3-restart", token)
    await comm1.send_json_to(make_lifecycle(
        edge_id="edge-m3-restart", monotonic_seq=1, event="session.online"))
    await comm1.receive_json_from()
    await comm1.send_json_to(make_lifecycle(
        edge_id="edge-m3-restart", monotonic_seq=2, event="task.running",
        task_id=task.id, task_code=task.code))
    await comm1.receive_json_from()
    await sync_to_async(node.refresh_from_db)()
    assert node.last_uplink_seq == 2
    await comm1.disconnect()

    # --- session 2: agent restarted, new stream restarts at seq=1 ---
    comm2 = WebsocketCommunicator(fleet_application, "/ws/fleet/")
    await comm2.connect()
    await _register(comm2, "edge-m3-restart", token)
    # register reset the high-water mark.
    await sync_to_async(node.refresh_from_db)()
    assert node.last_uplink_seq == 0

    # seq=1 again — old logic judged this DUPLICATE and silently dropped it.
    await comm2.send_json_to(make_lifecycle(
        edge_id="edge-m3-restart", monotonic_seq=1, event="task.running",
        task_id=task.id, task_code=task.code))
    ack = await comm2.receive_json_from()
    assert ack["type"] == FRAME_ACK

    await sync_to_async(node.refresh_from_db)()
    assert node.last_uplink_seq == 1  # advanced, not stuck at 2
    # The new stream's task.running was recorded — one row per session.
    running_rows = await sync_to_async(
        EdgeLifecycleEvent.objects.filter(edge=node, event="task.running").count
    )()
    assert running_rows == 2
    await comm2.disconnect()


@pytest.mark.asyncio
async def test_v02_task_state_pushes_browser_feed(_inmemory_channel_layer):
    """XIU-63 review MINOR: a v0.2 edge reports via task_state (not
    lifecycle); the change must still reach the browser /acquisition feed."""
    node, token = await sync_to_async(EdgeNode.issue)(name="edge-m3-v2push")
    task = await sync_to_async(_make_task)(code="m3-v2push", edge=node)

    browser = WebsocketCommunicator(fleet_application, "/ws/fleet/task-statuses/")
    connected, _ = await browser.connect()
    assert connected
    await browser.receive_json_from()  # snapshot

    edge = WebsocketCommunicator(fleet_application, "/ws/fleet/")
    await edge.connect()
    await _register(edge, "edge-m3-v2push", token)
    await edge.send_json_to(make_task_state(
        edge_id="edge-m3-v2push", task_id=task.id, task_code=task.code,
        state="running",
    ))
    await edge.receive_json_from()  # task_state ack

    pushed = await browser.receive_json_from()
    assert pushed["type"] == "task_status"
    assert pushed["data"]["task"] == task.id
    assert pushed["data"]["state"] == "running"

    await edge.disconnect()
    await browser.disconnect()


@pytest.mark.asyncio
async def test_uplink_frame_rejects_nonpositive_seq(_inmemory_channel_layer):
    """A monotonic_seq <= 0 is malformed — the center rejects the frame."""
    node, token = await sync_to_async(EdgeNode.issue)(name="edge-m3-badseq")

    comm = WebsocketCommunicator(fleet_application, "/ws/fleet/")
    await comm.connect()
    await _register(comm, "edge-m3-badseq", token)
    await comm.send_json_to({
        "v": PROTOCOL_VERSION, "type": "lifecycle", "edge_id": "edge-m3-badseq",
        "monotonic_seq": 0, "event": "session.online",
        "ts": "2026-05-22T03:00:00Z",
    })
    resp = await comm.receive_json_from()
    assert resp["type"] == FRAME_ERROR
    await comm.disconnect()


# ---------------------------------------------------------------------------
# REST projections
# ---------------------------------------------------------------------------


class TestRestProjections:
    def test_samples_endpoint_lists_cache(self):
        edge, _ = EdgeNode.issue(name="edge-m3-rest")
        task = _make_task(code="m3-rest", edge=edge)
        EdgeSample.objects.create(
            edge=edge, task=task, point_code="p0", value=3.3, quality="good",
        )
        resp = APIClient().get(f"/api/fleet/samples/?edge={edge.id}")
        assert resp.status_code == 200
        body = resp.json()
        rows = body["results"] if isinstance(body, dict) else body
        assert any(r["point_code"] == "p0" for r in rows)

    def test_lifecycle_events_endpoint_lists_timeline(self):
        edge, _ = EdgeNode.issue(name="edge-m3-rest2")
        EdgeLifecycleEvent.objects.create(
            edge=edge, event="session.online", monotonic_seq=1,
        )
        resp = APIClient().get(f"/api/fleet/lifecycle-events/?edge={edge.id}")
        assert resp.status_code == 200
        body = resp.json()
        rows = body["results"] if isinstance(body, dict) else body
        assert any(r["event"] == "session.online" for r in rows)
