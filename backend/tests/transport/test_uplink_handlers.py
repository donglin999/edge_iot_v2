"""Center-side data-plane uplink handlers — Phase 2 P5.1 (XIU-107).

Tests for :mod:`fleet.uplink_handlers` — the shared async handlers the
MQTT subscriber routes ``lifecycle`` / ``sample_batch`` / ``alarm_event``
frames through after XIU-107 closed the "no handler bound — dropping"
gap reported in [docs/distributed/m7-mqtt-acceptance.md §P5 acceptance
阻塞](../../../docs/distributed/m7-mqtt-acceptance.md).

The WS path covered by ``tests/test_fleet_m3.py`` / ``test_fleet_m4.py``
already exercises the recording side effects (``record_*`` coroutines);
this module focuses on the new MQTT-side wiring:

* ``handle_*`` resolves the edge by **name** (no PK on the wire).
* validates the frame and **logs+drops** bad fields (no socket to close).
* delegates to the same ``record_*`` write path as the WS consumer.
* :func:`fleet.uplink_handlers.install` binds all three on a router.
* a frame dispatched through :data:`fleet.uplink_router.default_router`
  reaches the DB (this is what was broken pre-XIU-107).
"""
from __future__ import annotations

import pytest

from fleet import uplink_handlers
from fleet.models import (
    EdgeLifecycleEvent,
    EdgeNode,
    EdgeSample,
    EdgeStatus,
    EdgeTaskStatus,
)
from fleet.protocol import (
    FRAME_ALARM_EVENT,
    FRAME_LIFECYCLE,
    FRAME_SAMPLE_BATCH,
)
from fleet.uplink_router import UplinkRouter, default_router


pytestmark = pytest.mark.django_db


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


@pytest.fixture
def disable_influx_mirror(monkeypatch):
    """The InfluxDB mirror is best-effort and not under test here. Turning
    the gate off keeps the test from importing the storage layer just to
    let it no-op back into the EdgeSample assertion path."""
    from django.conf import settings

    monkeypatch.setattr(settings, "CENTER_EDGE_SAMPLE_TO_INFLUX", False, raising=False)


# ---------------------------------------------------------------------------
# 1. handle_lifecycle: task.* event folds into EdgeTaskStatus
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_lifecycle_folds_task_event_into_status(_inmemory_channel_layer):
    """An inbound MQTT ``lifecycle`` task.* event creates the EdgeLifecycleEvent
    audit row AND folds the latest state into ``EdgeTaskStatus`` — same DB
    writes the WS path produced in M3, just driven through the shared
    ``handle_lifecycle`` coroutine."""
    from asgiref.sync import sync_to_async

    from configuration.models import AcqTask

    edge, _ = await sync_to_async(EdgeNode.issue)(name="edge-h-lifecycle")
    task = await sync_to_async(AcqTask.objects.create)(
        code="t-h-lifecycle", name="lifecycle task",
    )

    await uplink_handlers.handle_lifecycle("edge-h-lifecycle", {
        "type": FRAME_LIFECYCLE,
        "monotonic_seq": 1,
        "event": "task.running",
        "task_id": task.id,
        "task_code": task.code,
    })

    # Audit row + folded status both present.
    rows = await sync_to_async(
        lambda: list(EdgeLifecycleEvent.objects.filter(edge=edge))
    )()
    assert len(rows) == 1
    assert rows[0].event == "task.running"
    assert rows[0].monotonic_seq == 1

    status = await sync_to_async(EdgeTaskStatus.objects.get)(edge=edge, task=task)
    assert status.state == "running"

    refreshed = await sync_to_async(EdgeNode.objects.get)(pk=edge.pk)
    assert refreshed.last_uplink_seq == 1


@pytest.mark.asyncio
async def test_handle_lifecycle_drops_for_unknown_edge():
    """An LWT race or operator typo (frame for an edge we don't have a row
    for) must NOT auto-provision the edge — the handler logs+drops."""
    from asgiref.sync import sync_to_async

    await uplink_handlers.handle_lifecycle("ghost-edge", {
        "type": FRAME_LIFECYCLE,
        "monotonic_seq": 1,
        "event": "session.online",
    })

    assert not await sync_to_async(
        EdgeNode.objects.filter(name="ghost-edge").exists
    )()


@pytest.mark.asyncio
async def test_handle_lifecycle_drops_malformed_seq():
    """Missing/garbage ``monotonic_seq`` is logged+dropped without raising —
    the MQTT path never closes a socket on a bad frame."""
    from asgiref.sync import sync_to_async

    edge, _ = await sync_to_async(EdgeNode.issue)(name="edge-h-bad-seq")

    for bad in (
        {"type": FRAME_LIFECYCLE, "event": "session.online"},  # missing
        {"type": FRAME_LIFECYCLE, "monotonic_seq": "x", "event": "session.online"},
        {"type": FRAME_LIFECYCLE, "monotonic_seq": 0, "event": "session.online"},
    ):
        await uplink_handlers.handle_lifecycle("edge-h-bad-seq", bad)

    # Nothing recorded; the edge's high-water mark stays at 0.
    rows = await sync_to_async(
        lambda: list(EdgeLifecycleEvent.objects.filter(edge=edge))
    )()
    assert rows == []
    refreshed = await sync_to_async(EdgeNode.objects.get)(pk=edge.pk)
    assert refreshed.last_uplink_seq == 0


# ---------------------------------------------------------------------------
# 2. handle_sample_batch: writes EdgeSample, advances seq
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_sample_batch_writes_edge_samples(disable_influx_mirror):
    from asgiref.sync import sync_to_async

    from configuration.models import AcqTask

    edge, _ = await sync_to_async(EdgeNode.issue)(name="edge-h-sample")
    task = await sync_to_async(AcqTask.objects.create)(
        code="t-h-sample", name="sample task",
    )

    await uplink_handlers.handle_sample_batch("edge-h-sample", {
        "type": FRAME_SAMPLE_BATCH,
        "monotonic_seq": 1,
        "task_id": task.id,
        "task_code": task.code,
        "samples": [
            {"point_code": "POINT_A", "value": 1.5, "quality": "good"},
            {"point_code": "POINT_B", "value": 2, "quality": "good"},
        ],
    })

    samples = await sync_to_async(
        lambda: list(EdgeSample.objects.filter(edge=edge).order_by("point_code"))
    )()
    assert [s.point_code for s in samples] == ["POINT_A", "POINT_B"]
    assert samples[0].value == 1.5

    refreshed = await sync_to_async(EdgeNode.objects.get)(pk=edge.pk)
    assert refreshed.last_uplink_seq == 1


@pytest.mark.asyncio
async def test_handle_sample_batch_duplicate_seq_is_dropped(disable_influx_mirror):
    """The recording layer's ``monotonic_seq`` dedup applies regardless of
    transport — a redelivered MQTT frame (QoS 1) lands as a no-op."""
    from asgiref.sync import sync_to_async

    from configuration.models import AcqTask

    edge, _ = await sync_to_async(EdgeNode.issue)(name="edge-h-dup")
    task = await sync_to_async(AcqTask.objects.create)(
        code="t-h-dup", name="dup task",
    )

    frame = {
        "type": FRAME_SAMPLE_BATCH,
        "monotonic_seq": 1,
        "task_id": task.id,
        "task_code": task.code,
        "samples": [{"point_code": "P", "value": 1.0}],
    }

    await uplink_handlers.handle_sample_batch("edge-h-dup", frame)
    await uplink_handlers.handle_sample_batch("edge-h-dup", frame)  # replay

    count = await sync_to_async(EdgeSample.objects.filter(edge=edge).count)()
    assert count == 1
    refreshed = await sync_to_async(EdgeNode.objects.get)(pk=edge.pk)
    assert refreshed.last_uplink_seq == 1


# ---------------------------------------------------------------------------
# 3. handle_alarm_event: opens Alarm row tagged with the source edge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_alarm_event_opens_alarm_row():
    from asgiref.sync import sync_to_async

    from acquisition.models import Alarm, AlarmRule

    edge, _ = await sync_to_async(EdgeNode.issue)(name="edge-h-alarm")
    rule = await sync_to_async(AlarmRule.objects.create)(
        name="mqtt rule", point_code="P", operator="gt", threshold=10.0,
    )

    await uplink_handlers.handle_alarm_event("edge-h-alarm", {
        "type": FRAME_ALARM_EVENT,
        "monotonic_seq": 1,
        "rule_id": rule.id,
        "point_code": "P",
        "device_code": "plc-1",
        "value": 42.0,
        "message": "P=42 触发",
        "status": "firing",
    })

    alarm = await sync_to_async(
        lambda: Alarm.objects.select_related("edge").get(edge=edge)
    )()
    assert alarm.rule_id == rule.id
    assert alarm.value == 42.0
    assert alarm.status == Alarm.STATUS_FIRING

    refreshed = await sync_to_async(EdgeNode.objects.get)(pk=edge.pk)
    assert refreshed.last_uplink_seq == 1


# ---------------------------------------------------------------------------
# 4. Router wiring — install() makes default_router route the three types
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_binds_handlers_on_router(disable_influx_mirror):
    """:func:`install` makes the router route data-plane frames to the
    shared handlers — the core gap XIU-107 closes."""
    from asgiref.sync import sync_to_async

    from configuration.models import AcqTask

    edge, _ = await sync_to_async(EdgeNode.issue)(name="edge-h-router")
    task = await sync_to_async(AcqTask.objects.create)(
        code="t-h-router", name="router task",
    )

    router = UplinkRouter()
    uplink_handlers.install(router)

    # Lifecycle.
    await router.dispatch("edge-h-router", {
        "type": FRAME_LIFECYCLE,
        "monotonic_seq": 1,
        "event": "task.starting",
        "task_id": task.id,
        "task_code": task.code,
    })
    # Sample batch.
    await router.dispatch("edge-h-router", {
        "type": FRAME_SAMPLE_BATCH,
        "monotonic_seq": 2,
        "task_id": task.id,
        "task_code": task.code,
        "samples": [{"point_code": "PX", "value": 1}],
    })

    status = await sync_to_async(
        EdgeTaskStatus.objects.get
    )(edge=edge, task=task)
    assert status.state == "starting"

    samples = await sync_to_async(
        lambda: list(EdgeSample.objects.filter(edge=edge))
    )()
    assert len(samples) == 1 and samples[0].point_code == "PX"

    refreshed = await sync_to_async(EdgeNode.objects.get)(pk=edge.pk)
    assert refreshed.last_uplink_seq == 2


@pytest.mark.asyncio
async def test_default_router_has_handlers_after_apps_ready(
    disable_influx_mirror,
):
    """``FleetConfig.ready`` calls ``uplink_handlers.install()`` so the
    process-wide ``default_router`` has the three type handlers bound by
    the time the MQTT subscriber thread starts. The autouse Django setup
    in conftest already invoked ``ready``, so we just re-install onto the
    default router (idempotent) and dispatch a frame end-to-end.

    This is the regression assertion behind the issue: an MQTT frame
    dispatched through ``default_router`` must NOT fall into the
    "no handler bound — dropping" branch in :class:`UplinkRouter`.
    """
    from asgiref.sync import sync_to_async

    from configuration.models import AcqTask

    edge, _ = await sync_to_async(EdgeNode.issue)(name="edge-h-default")
    task = await sync_to_async(AcqTask.objects.create)(
        code="t-h-default", name="default-router task",
    )

    # Idempotent re-install. (``ready`` may not have run during the test
    # session depending on lazy app loading, so this guarantees the bind.)
    uplink_handlers.install(default_router)

    await default_router.dispatch("edge-h-default", {
        "type": FRAME_SAMPLE_BATCH,
        "monotonic_seq": 1,
        "task_id": task.id,
        "task_code": task.code,
        "samples": [{"point_code": "PD", "value": 7}],
    })

    samples = await sync_to_async(
        lambda: list(EdgeSample.objects.filter(edge=edge))
    )()
    assert len(samples) == 1
    assert samples[0].point_code == "PD"


# ---------------------------------------------------------------------------
# 5. Backfill stamping — backfill=True touches last_backfill_at
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_backfilled_sample_stamps_last_backfill_at(
    disable_influx_mirror,
):
    from asgiref.sync import sync_to_async

    from configuration.models import AcqTask

    edge, _ = await sync_to_async(EdgeNode.issue)(name="edge-h-backfill")
    task = await sync_to_async(AcqTask.objects.create)(
        code="t-h-backfill", name="backfill task",
    )

    await uplink_handlers.handle_sample_batch("edge-h-backfill", {
        "type": FRAME_SAMPLE_BATCH,
        "monotonic_seq": 1,
        "task_id": task.id,
        "task_code": task.code,
        "samples": [{"point_code": "PB", "value": 1}],
        "backfill": True,
    })

    refreshed = await sync_to_async(EdgeNode.objects.get)(pk=edge.pk)
    assert refreshed.last_backfill_at is not None


# ---------------------------------------------------------------------------
# Shared fixture — the M3/M4 WS tests use this name, so re-define so we
# don't drag the channels in-memory layer just to mute it.
# ---------------------------------------------------------------------------


@pytest.fixture
def _inmemory_channel_layer(settings):
    """No-op channel layer so broadcast_task_status' ``group_send`` is silent.

    ``broadcast_task_status`` is invoked inside ``record_lifecycle`` after a
    successful task.* fold. It silently swallows a missing layer, but on
    sqlite-on-disk + asyncio we get cleaner output if we hand it a real
    in-memory layer to drain into.
    """
    settings.CHANNEL_LAYERS = {
        "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
    }
    yield
