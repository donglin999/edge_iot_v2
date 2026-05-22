"""Tests for the M4 alarm layer of the fleet/ app (XIU-69).

Covers:
- ``make_alarm_event`` frame builder + ``make_apply_config`` alarm_rules field
- ``build_apply_config_payload`` carrying the active alarm-rule set
- WS ingest of ``alarm_event`` frames → ``acquisition.Alarm`` rows with the
  source ``edge`` FK set (距 — "哪个 edge 报的告警")
- per-edge ``last_uplink_seq`` advancement / duplicate drop / unknown-rule drop
- idempotent ``(rule, edge, point)`` dedup of repeated fires
- rule CRUD → automatic re-push of ``apply_config`` to online edges
- the shared ``evaluate_readings`` threshold logic the edge reuses
- the AlarmSink → uplink ``alarm_event`` hook plumbing
- the ``/api/acquisition/alarms/`` projection exposing ``edge`` / ``edge_name``
"""
from __future__ import annotations

import pytest
from asgiref.sync import sync_to_async
from channels.layers import channel_layers
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.conf import settings
from rest_framework.test import APIClient

from acquisition.models import Alarm, AlarmRule
from acquisition.services import uplink as acq_uplink
from acquisition.services.alarms import evaluate_readings
from acquisition.services.sinks import AlarmSink
from configuration.models import AcqTask, Device, Point, PointTemplate, Site
from fleet.models import EdgeNode
from fleet.protocol import (
    FRAME_ACK,
    FRAME_ERROR,
    PROTOCOL_VERSION,
    make_alarm_event,
    make_apply_config,
    make_register,
)
from fleet.routing import websocket_urlpatterns
from fleet.services import build_apply_config_payload, sync_alarm_rules_to_edges

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
    point = Point.objects.create(
        device=device, template=tpl, code=f"{code}-p0", address="40001"
    )
    task.points.add(point)
    return task


async def _register(comm, edge_name: str, token: str) -> dict:
    await comm.send_json_to(make_register(edge_id=edge_name, token=token, version="0.4.0"))
    return await comm.receive_json_from()


# ---------------------------------------------------------------------------
# frame builders
# ---------------------------------------------------------------------------


class TestAlarmFrameBuilders:
    def test_make_alarm_event_shape(self):
        frame = make_alarm_event(
            edge_id="e1", monotonic_seq=12, rule_id=3, point_code="holding_0",
            value=137.0, device_code="plc-1", severity="warning",
            message="holding_0=137.0 > 100.0",
        )
        assert frame["v"] == PROTOCOL_VERSION
        assert frame["type"] == "alarm_event"
        assert frame["monotonic_seq"] == 12
        assert frame["rule_id"] == 3
        assert frame["point_code"] == "holding_0"
        assert frame["value"] == 137.0
        assert frame["status"] == "firing"
        assert "fired_at" in frame

    def test_make_alarm_event_rejects_bad_status(self):
        with pytest.raises(ValueError):
            make_alarm_event(
                edge_id="e1", monotonic_seq=1, rule_id=1, point_code="p0",
                value=1, status="exploded",
            )

    def test_make_apply_config_carries_alarm_rules(self):
        rules = [{"id": 1, "point_code": "p0", "operator": "gt", "threshold": 10}]
        frame = make_apply_config(
            version=4, tasks=[], devices=[], points=[], alarm_rules=rules,
        )
        assert frame["alarm_rules"] == rules
        # Field is always present even when omitted by the caller.
        bare = make_apply_config(version=4, tasks=[], devices=[], points=[])
        assert bare["alarm_rules"] == []


# ---------------------------------------------------------------------------
# build_apply_config_payload — alarm rules ride the snapshot
# ---------------------------------------------------------------------------


class TestApplyConfigAlarmRules:
    def test_active_rules_ride_the_snapshot(self):
        edge, _ = EdgeNode.issue(name="edge-m4-cfg")
        _make_task(code="m4-cfg", edge=edge)
        active = AlarmRule.objects.create(
            name="holding high", point_code="m4-cfg-p0", operator="gt",
            threshold=100.0, severity="warning", is_active=True,
        )
        disabled = AlarmRule.objects.create(
            name="disabled rule", point_code="m4-cfg-p0", operator="gt",
            threshold=5.0, is_active=False,
        )
        frame = build_apply_config_payload(edge, version=1)
        by_id = {r["id"]: r for r in frame["alarm_rules"]}
        # The active rule rides the snapshot...
        assert active.id in by_id
        assert by_id[active.id]["threshold"] == 100.0
        # ...the inactive one does not — the edge culls anything absent.
        assert disabled.id not in by_id


# ---------------------------------------------------------------------------
# WS ingest — alarm_event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alarm_event_creates_alarm_with_source_edge(_inmemory_channel_layer):
    """An inbound alarm_event lands as an acquisition.Alarm tagged with the
    reporting edge — that is what the /alarms page reads for 来源 edge."""
    node, token = await sync_to_async(EdgeNode.issue)(name="edge-m4-evt")
    rule = await sync_to_async(AlarmRule.objects.create)(
        name="evt rule", point_code="holding_0", operator="gt", threshold=100.0,
    )

    comm = WebsocketCommunicator(fleet_application, "/ws/fleet/")
    connected, _ = await comm.connect()
    assert connected
    await _register(comm, "edge-m4-evt", token)

    await comm.send_json_to(make_alarm_event(
        edge_id="edge-m4-evt", monotonic_seq=1, rule_id=rule.id,
        point_code="holding_0", value=137.0, device_code="plc-1",
        message="holding_0=137.0 触发规则",
    ))
    ack = await comm.receive_json_from()
    assert ack["type"] == FRAME_ACK and ack["ref"] == "alarm_event"

    alarm = await sync_to_async(
        lambda: Alarm.objects.select_related("edge", "rule").get(edge=node)
    )()
    assert alarm.rule_id == rule.id
    assert alarm.point_code == "holding_0"
    assert alarm.device_code == "plc-1"
    assert alarm.value == 137.0
    assert alarm.status == Alarm.STATUS_FIRING
    assert alarm.edge_id == node.id

    await sync_to_async(node.refresh_from_db)()
    assert node.last_uplink_seq == 1

    await comm.disconnect()


@pytest.mark.asyncio
async def test_alarm_event_duplicate_seq_is_dropped(_inmemory_channel_layer):
    node, token = await sync_to_async(EdgeNode.issue)(name="edge-m4-dup")
    rule = await sync_to_async(AlarmRule.objects.create)(
        name="dup rule", point_code="p0", operator="gt", threshold=1.0,
    )

    comm = WebsocketCommunicator(fleet_application, "/ws/fleet/")
    await comm.connect()
    await _register(comm, "edge-m4-dup", token)

    # Two advancing events on distinct points → 2 alarms, seq at 2.
    for seq, point in ((1, "p0"), (2, "p1")):
        await comm.send_json_to(make_alarm_event(
            edge_id="edge-m4-dup", monotonic_seq=seq, rule_id=rule.id,
            point_code=point, value=9.0,
        ))
        await comm.receive_json_from()

    # Replay seq=1 — duplicate, must be dropped (no third alarm row).
    await comm.send_json_to(make_alarm_event(
        edge_id="edge-m4-dup", monotonic_seq=1, rule_id=rule.id,
        point_code="p0", value=9.0,
    ))
    ack = await comm.receive_json_from()
    assert ack["type"] == FRAME_ACK

    count = await sync_to_async(Alarm.objects.filter(edge=node).count)()
    assert count == 2
    await sync_to_async(node.refresh_from_db)()
    assert node.last_uplink_seq == 2

    await comm.disconnect()


@pytest.mark.asyncio
async def test_alarm_event_unknown_rule_dropped_seq_advances(_inmemory_channel_layer):
    """A rule deleted center-side between fire and receipt → alarm dropped,
    but the uplink stream still advances so later frames are not gaps."""
    node, token = await sync_to_async(EdgeNode.issue)(name="edge-m4-norule")

    comm = WebsocketCommunicator(fleet_application, "/ws/fleet/")
    await comm.connect()
    await _register(comm, "edge-m4-norule", token)

    await comm.send_json_to(make_alarm_event(
        edge_id="edge-m4-norule", monotonic_seq=1, rule_id=999999,
        point_code="p0", value=1.0,
    ))
    ack = await comm.receive_json_from()
    assert ack["type"] == FRAME_ACK

    count = await sync_to_async(Alarm.objects.filter(edge=node).count)()
    assert count == 0
    await sync_to_async(node.refresh_from_db)()
    assert node.last_uplink_seq == 1  # advanced despite the drop

    await comm.disconnect()


@pytest.mark.asyncio
async def test_alarm_event_idempotent_dedup(_inmemory_channel_layer):
    """A repeated fire of the same (rule, edge, point) — e.g. an edge restart
    re-firing — must not pile up duplicate open Alarm rows."""
    node, token = await sync_to_async(EdgeNode.issue)(name="edge-m4-idem")
    rule = await sync_to_async(AlarmRule.objects.create)(
        name="idem rule", point_code="holding_0", operator="gt", threshold=100.0,
    )

    comm = WebsocketCommunicator(fleet_application, "/ws/fleet/")
    await comm.connect()
    await _register(comm, "edge-m4-idem", token)

    # Two advancing alarm_events for the same rule+point (seq 1, then 2).
    for seq in (1, 2):
        await comm.send_json_to(make_alarm_event(
            edge_id="edge-m4-idem", monotonic_seq=seq, rule_id=rule.id,
            point_code="holding_0", value=150.0,
        ))
        await comm.receive_json_from()

    count = await sync_to_async(
        Alarm.objects.filter(edge=node, rule=rule, point_code="holding_0",
                             status=Alarm.STATUS_FIRING).count
    )()
    assert count == 1  # one open alarm, not two

    await comm.disconnect()


@pytest.mark.asyncio
async def test_alarm_event_rejects_nonpositive_seq(_inmemory_channel_layer):
    node, token = await sync_to_async(EdgeNode.issue)(name="edge-m4-badseq")

    comm = WebsocketCommunicator(fleet_application, "/ws/fleet/")
    await comm.connect()
    await _register(comm, "edge-m4-badseq", token)
    await comm.send_json_to({
        "v": PROTOCOL_VERSION, "type": "alarm_event", "edge_id": "edge-m4-badseq",
        "monotonic_seq": 0, "rule_id": 1, "point_code": "p0", "value": 1,
    })
    resp = await comm.receive_json_from()
    assert resp["type"] == FRAME_ERROR
    await comm.disconnect()


# ---------------------------------------------------------------------------
# rule CRUD → automatic re-sync to online edges
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rule_change_repushes_apply_config_to_online_edge(_inmemory_channel_layer):
    """sync_alarm_rules_to_edges re-pushes a fresh apply_config — carrying the
    updated threshold — to every online edge (the 改阈值→自动重下发 path)."""
    node, token = await sync_to_async(EdgeNode.issue)(name="edge-m4-resync")
    await sync_to_async(_make_task)(code="m4-resync", edge=node)
    rule = await sync_to_async(AlarmRule.objects.create)(
        name="resync rule", point_code="m4-resync-p0", operator="gt",
        threshold=100.0,
    )

    comm = WebsocketCommunicator(fleet_application, "/ws/fleet/")
    await comm.connect()
    await _register(comm, "edge-m4-resync", token)  # marks the edge online

    # Operator raises the threshold, then triggers the re-sync.
    rule.threshold = 250.0
    await sync_to_async(rule.save)(update_fields=["threshold"])
    summaries = await sync_to_async(sync_alarm_rules_to_edges)()
    # This edge is among the online edges that got a fresh apply_config.
    assert any(s.get("edge_name") == "edge-m4-resync" for s in summaries)

    # The edge's WS receives the re-pushed apply_config with the new threshold.
    frame = await comm.receive_json_from()
    assert frame["type"] == "apply_config"
    rule_payloads = {r["id"]: r for r in frame["alarm_rules"]}
    assert rule_payloads[rule.id]["threshold"] == 250.0

    await comm.disconnect()


def test_alarm_rule_save_schedules_resync(django_capture_on_commit_callbacks):
    """The post_save signal on AlarmRule registers an on-commit re-sync —
    so an admin/Excel/shell edit re-pushes rules without a manual sync."""
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        AlarmRule.objects.create(
            name="signal rule", point_code="p0", operator="gt", threshold=1.0,
        )
    assert len(callbacks) >= 1


# ---------------------------------------------------------------------------
# shared evaluate_readings logic + AlarmSink uplink hook (edge 本地评估)
# ---------------------------------------------------------------------------


class TestEdgeLocalEvaluation:
    def test_evaluate_readings_fires_over_threshold(self):
        """The shared threshold logic the edge AlarmSink reuses (import, not
        copy) creates an Alarm when a reading breaches the rule."""
        # Unique point_code: WS/async tests in this module commit rows that
        # outlive their rollback, so a generic code would match stray rules.
        pt = "m4eval_holding"
        rule = AlarmRule.objects.create(
            name="over", point_code=pt, operator="gt", threshold=100.0,
        )
        fired = evaluate_readings(
            None, "plc-1",
            [{"code": pt, "value": 137.0, "quality": "good"}],
        )
        assert len(fired) == 1
        assert fired[0].rule_id == rule.id
        assert fired[0].point_code == pt
        # An in-range reading fires nothing.
        assert evaluate_readings(
            None, "plc-1",
            [{"code": pt, "value": 50.0, "quality": "good"}],
        ) == []

    def test_alarm_sink_emits_uplink_event_for_fired_alarm(self):
        """AlarmSink relays each fired alarm to the uplink hook — the path
        the edge-agent turns into an ``alarm_event`` frame."""
        pt = "m4sink_holding"
        rule = AlarmRule.objects.create(
            name="sink", point_code=pt, operator="gt", threshold=10.0,
            severity="critical",
        )
        fired = evaluate_readings(
            None, "plc-9",
            [{"code": pt, "value": 42.0, "quality": "good"}],
        )
        assert len(fired) == 1

        captured = []
        acq_uplink.register_alarm_hook(captured.append)
        try:
            assert acq_uplink.has_alarm_hook()
            AlarmSink._emit_alarm_events(fired)
        finally:
            acq_uplink.clear_alarm_hook()

        assert len(captured) == 1
        event = captured[0]
        assert isinstance(event, acq_uplink.AlarmEvent)
        assert event.rule_id == rule.id
        assert event.point_code == pt
        assert event.value == 42.0
        assert event.severity == "critical"

    def test_emit_alarm_is_noop_without_hook(self):
        """Monolith deployment: no hook registered → emit is a cheap no-op."""
        acq_uplink.clear_alarm_hook()
        assert not acq_uplink.has_alarm_hook()
        # Must not raise.
        acq_uplink.emit_alarm(acq_uplink.AlarmEvent(
            rule_id=1, point_code="p0", device_code="", value=1,
        ))


# ---------------------------------------------------------------------------
# REST projection — /alarms exposes the source edge
# ---------------------------------------------------------------------------


class TestAlarmRestProjection:
    def test_alarms_endpoint_exposes_edge_and_edge_name(self):
        edge, _ = EdgeNode.issue(name="edge-m4-rest")
        rule = AlarmRule.objects.create(
            name="rest rule", point_code="p0", operator="gt", threshold=1.0,
        )
        Alarm.objects.create(
            rule=rule, edge=edge, point_code="p0", device_code="d0", value=9.0,
            status=Alarm.STATUS_FIRING,
        )
        resp = APIClient().get(f"/api/acquisition/alarms/?edge={edge.id}")
        assert resp.status_code == 200
        body = resp.json()
        rows = body["results"] if isinstance(body, dict) else body
        assert len(rows) == 1
        assert rows[0]["edge"] == edge.id
        assert rows[0]["edge_name"] == "edge-m4-rest"

    def test_alarms_endpoint_edge_name_null_for_local_alarm(self):
        rule = AlarmRule.objects.create(
            name="local rule", point_code="p0", operator="gt", threshold=1.0,
        )
        Alarm.objects.create(
            rule=rule, point_code="p0", value=9.0, status=Alarm.STATUS_FIRING,
        )
        resp = APIClient().get("/api/acquisition/alarms/")
        assert resp.status_code == 200
        body = resp.json()
        rows = body["results"] if isinstance(body, dict) else body
        local = [r for r in rows if r["edge"] is None]
        assert local and local[0]["edge_name"] is None
