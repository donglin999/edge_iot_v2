"""Tests for the M2 config-dispatch layer of the fleet/ app (XIU-59).

Covers:
- EdgeAssignment model + reconcile_assignments version bumping
- build_apply_config_payload snapshot shape
- POST /api/fleet/edges/<id>/assignments/sync
- AcqTask.edge_id round-trips through the DRF task serializer
- WS ingest of task_state / config_applied frames into EdgeTaskStatus
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from asgiref.sync import sync_to_async
from channels.layers import channel_layers
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.conf import settings
from rest_framework.test import APIClient

from configuration.models import AcqTask, Device, Point, PointTemplate, Site
from fleet.models import (
    AssignmentDesiredState,
    EdgeAssignment,
    EdgeNode,
    EdgeTaskStatus,
)
from fleet.protocol import (
    FRAME_ACK,
    PROTOCOL_VERSION,
    make_config_applied,
    make_register,
    make_task_state,
)
from fleet.routing import websocket_urlpatterns
from fleet.services import build_apply_config_payload, reconcile_assignments, sync_assignments


pytestmark = pytest.mark.django_db


fleet_application = URLRouter(websocket_urlpatterns)


@pytest.fixture(autouse=True)
def _patch_db_defaults():
    """Mirror test_fleet.py — re-inject Django 4.2 connection defaults the
    session-scoped conftest fixture drops, so database_sync_to_async works."""
    db = settings.DATABASES["default"]
    db.setdefault("TIME_ZONE", None)
    db.setdefault("CONN_HEALTH_CHECKS", False)
    db.setdefault("CONN_MAX_AGE", 0)
    db.setdefault("AUTOCOMMIT", True)
    db.setdefault("OPTIONS", {})
    yield


@pytest.fixture
def _inmemory_channel_layer():
    """Swap the Redis channel layer for an in-memory one for WS tests.

    The M2 consumer joins a per-edge group on register; without this the
    test would need a live Redis. We reset the cached layer registry so
    each test gets a fresh in-memory layer.
    """
    original = settings.CHANNEL_LAYERS
    settings.CHANNEL_LAYERS = {
        "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
    }
    channel_layers.backends = {}
    yield
    settings.CHANNEL_LAYERS = original
    channel_layers.backends = {}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_task_with_points(*, code: str, edge: EdgeNode | None, n_points: int = 2) -> AcqTask:
    site, _ = Site.objects.get_or_create(code="default", defaults={"name": "default"})
    device = Device.objects.create(
        site=site,
        code=f"dev-{code}",
        name=f"Device {code}",
        protocol="modbus_tcp",
        ip_address="127.0.0.1",
        port=5020,
        metadata={},
    )
    tpl = PointTemplate.objects.create(
        name=f"tpl-{code}", english_name=f"tpl-{code}", unit="", data_type="uint16"
    )
    task = AcqTask.objects.create(code=code, name=f"Task {code}", edge=edge)
    for i in range(n_points):
        point = Point.objects.create(
            device=device, template=tpl, code=f"{code}-p{i}", address=str(40001 + i)
        )
        task.points.add(point)
    return task


# ---------------------------------------------------------------------------
# EdgeAssignment model + reconcile
# ---------------------------------------------------------------------------


class TestEdgeAssignment:
    def test_reconcile_creates_assignment_and_bumps_version(self):
        edge, _ = EdgeNode.issue(name="edge-a")
        _make_task_with_points(code="task-a", edge=edge)

        version, assignments = reconcile_assignments(edge)

        assert version == 1
        assert len(assignments) == 1
        a = assignments[0]
        assert a.config_version == 1
        assert a.desired_state == AssignmentDesiredState.RUNNING
        assert a.last_applied_version is None

    def test_reconcile_drops_assignment_when_task_unassigned(self):
        edge, _ = EdgeNode.issue(name="edge-b")
        task = _make_task_with_points(code="task-b", edge=edge)
        reconcile_assignments(edge)
        assert EdgeAssignment.objects.filter(edge=edge).count() == 1

        # Task moved off the edge → reconcile should drop the row.
        task.edge = None
        task.save(update_fields=["edge"])
        version, assignments = reconcile_assignments(edge)

        assert version == 2
        assert assignments == []
        assert EdgeAssignment.objects.filter(edge=edge).count() == 0

    def test_reconcile_version_increments_monotonically(self):
        edge, _ = EdgeNode.issue(name="edge-c")
        _make_task_with_points(code="task-c", edge=edge)

        v1, _ = reconcile_assignments(edge)
        v2, _ = reconcile_assignments(edge)
        v3, _ = reconcile_assignments(edge)

        assert [v1, v2, v3] == [1, 2, 3]


# ---------------------------------------------------------------------------
# apply_config payload builder
# ---------------------------------------------------------------------------


class TestApplyConfigPayload:
    def test_payload_includes_tasks_devices_points(self):
        edge, _ = EdgeNode.issue(name="edge-d")
        task = _make_task_with_points(code="task-d", edge=edge, n_points=3)

        frame = build_apply_config_payload(edge, version=5)

        assert frame["v"] == PROTOCOL_VERSION
        assert frame["type"] == "apply_config"
        assert frame["version"] == 5
        assert len(frame["tasks"]) == 1
        assert frame["tasks"][0]["id"] == task.id
        assert len(frame["tasks"][0]["point_ids"]) == 3
        assert len(frame["devices"]) == 1
        assert len(frame["points"]) == 3
        # Each point carries an inline template so the edge needs no follow-up.
        assert frame["points"][0]["template"] is not None

    def test_payload_excludes_tasks_owned_by_other_edges(self):
        edge_x, _ = EdgeNode.issue(name="edge-x")
        edge_y, _ = EdgeNode.issue(name="edge-y")
        _make_task_with_points(code="on-x", edge=edge_x)
        _make_task_with_points(code="on-y", edge=edge_y)

        frame = build_apply_config_payload(edge_x, version=1)

        codes = {t["code"] for t in frame["tasks"]}
        assert codes == {"on-x"}


# ---------------------------------------------------------------------------
# sync endpoint + AcqTask.edge_id via DRF
# ---------------------------------------------------------------------------


class TestSyncEndpointAndSerializer:
    def test_sync_endpoint_returns_summary(self):
        edge, _ = EdgeNode.issue(name="edge-sync")
        _make_task_with_points(code="task-sync", edge=edge)

        client = APIClient()
        resp = client.post(f"/api/fleet/edges/{edge.id}/assignments/sync/")

        assert resp.status_code == 200
        body = resp.json()
        assert body["edge_id"] == edge.id
        assert body["config_version"] == 1
        assert body["assignment_count"] == 1
        assert body["device_count"] == 1
        assert body["point_count"] == 2
        # `delivered` only reflects whether the channel layer accepted the
        # frame — it is best-effort and depends on the layer backend being
        # reachable, so we only assert it is a bool.
        assert isinstance(body["delivered"], bool)
        assert body["frame"]["type"] == "apply_config"

    def test_patch_task_edge_id_assigns_edge(self):
        edge, _ = EdgeNode.issue(name="edge-patch")
        task = _make_task_with_points(code="task-patch", edge=None)
        assert task.edge_id is None

        client = APIClient()
        resp = client.patch(
            f"/api/config/tasks/{task.id}/",
            data={"edge_id": edge.id},
            format="json",
        )

        assert resp.status_code == 200
        assert resp.json()["edge_id"] == edge.id
        task.refresh_from_db()
        assert task.edge_id == edge.id

    def test_patch_task_edge_id_null_clears_edge(self):
        edge, _ = EdgeNode.issue(name="edge-clear")
        task = _make_task_with_points(code="task-clear", edge=edge)

        client = APIClient()
        resp = client.patch(
            f"/api/config/tasks/{task.id}/",
            data={"edge_id": None},
            format="json",
        )

        assert resp.status_code == 200
        task.refresh_from_db()
        assert task.edge_id is None


# ---------------------------------------------------------------------------
# WS ingest — task_state / config_applied
# ---------------------------------------------------------------------------


async def _register(comm, edge_name: str, token: str) -> dict:
    await comm.send_json_to(make_register(edge_id=edge_name, token=token, version="0.2.0"))
    return await comm.receive_json_from()


@pytest.mark.asyncio
async def test_task_state_frame_creates_edge_task_status(_inmemory_channel_layer):
    edge_name = "edge-ws-state"
    node, token = await sync_to_async(EdgeNode.issue)(name=edge_name)
    task = await sync_to_async(_make_task_with_points)(code="ws-task", edge=node)

    comm = WebsocketCommunicator(fleet_application, "/ws/fleet/")
    connected, _ = await comm.connect()
    assert connected

    ack = await _register(comm, edge_name, token)
    assert ack["type"] == FRAME_ACK

    await comm.send_json_to(
        make_task_state(
            edge_id=edge_name,
            task_id=task.id,
            task_code=task.code,
            state="running",
        )
    )
    ack = await comm.receive_json_from()
    assert ack["type"] == FRAME_ACK
    assert ack["ref"] == "task_state"

    status = await sync_to_async(EdgeTaskStatus.objects.get)(edge=node, task=task)
    assert status.state == "running"

    await comm.disconnect()


@pytest.mark.asyncio
async def test_config_applied_frame_stamps_assignment(_inmemory_channel_layer):
    edge_name = "edge-ws-applied"
    node, token = await sync_to_async(EdgeNode.issue)(name=edge_name)
    await sync_to_async(_make_task_with_points)(code="applied-task", edge=node)
    await sync_to_async(reconcile_assignments)(node)

    comm = WebsocketCommunicator(fleet_application, "/ws/fleet/")
    connected, _ = await comm.connect()
    assert connected
    await _register(comm, edge_name, token)

    await comm.send_json_to(
        make_config_applied(edge_id=edge_name, version=1, status="ok")
    )
    ack = await comm.receive_json_from()
    assert ack["type"] == FRAME_ACK
    assert ack["ref"] == "config_applied"

    assignment = await sync_to_async(
        lambda: EdgeAssignment.objects.get(edge=node)
    )()
    assert assignment.last_applied_version == 1
    assert assignment.applied_at is not None

    await comm.disconnect()
