"""WebSocket route, frame and browser reconnect compatibility contract."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync
from django.conf import settings
from django.utils import timezone

from acquisition import consumers, models as acquisition_models, routing
from acquisition.services.read_plan import Reading
from acquisition.services.sinks import WebSocketSink
from configuration import models as configuration_models

from .helpers import REPOSITORY_ROOT, load_contract


_NON_EVENT_PUBLIC_METHODS = {
    "AcquisitionConsumer": {
        "connect", "disconnect", "receive", "get_session_status",
    },
    "GlobalAcquisitionConsumer": {
        "connect", "disconnect", "receive", "get_active_sessions",
    },
}


class _Recorder:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, *, text_data: str) -> None:
        self.messages.append(json.loads(text_data))


class _ChannelLayer:
    def __init__(self) -> None:
        self.groups: list[tuple[str, str]] = []
        self.sent: list[tuple[str, dict]] = []

    async def group_add(self, group: str, channel: str) -> None:
        self.groups.append((group, channel))

    async def group_send(self, group: str, message: dict) -> None:
        self.sent.append((group, message))


def _dispatch(method, event: dict) -> dict:
    recorder = _Recorder()
    asyncio.run(method(recorder, event))
    assert len(recorder.messages) == 1
    return recorder.messages[0]


def test_websocket_routes_and_channel_delivery_window() -> None:
    contract = load_contract("websocket-v1.json")
    patterns = [str(pattern.pattern) for pattern in routing.websocket_urlpatterns]
    assert patterns == [item["django_regex"] for item in contract["paths"]]

    channel = settings.CHANNEL_LAYERS["default"]
    assert channel["BACKEND"] == contract["transport"]["channel_backend"]
    layer = channel["CONFIG"]
    assert layer["expiry"] == contract["transport"]["channel_message_expiry_seconds"]
    assert layer["capacity"] == contract["transport"]["channel_capacity"]


def test_outbound_event_variants_match_frozen_envelopes() -> None:
    contract = load_contract("websocket-v1.json")
    classes = {
        "AcquisitionConsumer": consumers.AcquisitionConsumer,
        "GlobalAcquisitionConsumer": consumers.GlobalAcquisitionConsumer,
    }
    payload = {"sentinel": "contract"}
    alarm = {"id": 99, "message": "synthetic"}

    expected_handlers = {
        (variant["consumer"], variant["handler"])
        for variant in contract["forwarded_events"]
    }
    actual_handlers = set()
    for class_name, consumer_class in classes.items():
        public_callables = {
            name
            for name, value in consumer_class.__dict__.items()
            if not name.startswith("_") and callable(value)
        }
        non_events = _NON_EVENT_PUBLIC_METHODS[class_name]
        assert non_events <= public_callables
        actual_handlers.update(
            (class_name, name) for name in public_callables - non_events
        )
    assert actual_handlers == expected_handlers

    for variant in contract["forwarded_events"]:
        method = getattr(classes[variant["consumer"]], variant["handler"])
        event = {"data": payload, "event": "created", "alarm": alarm}
        frame = _dispatch(method, event)
        assert frame == variant["example_frame"]
        for key in ("event_id", "sequence", "resume_cursor"):
            assert contract["transport"][key] is False
            assert key not in frame


@pytest.mark.django_db(transaction=True)
def test_both_on_connect_frames_are_built_by_production_consumers(monkeypatch) -> None:
    contract = load_contract("websocket-v1.json")
    task = configuration_models.AcqTask.objects.create(
        code="ws-contract-task",
        name="WS Contract Task",
    )
    session = acquisition_models.AcquisitionSession.objects.create(
        task=task,
        status=acquisition_models.AcquisitionSession.STATUS_RUNNING,
        started_at=timezone.now(),
    )
    original_cache = consumers._status_cache
    original_cache_snapshot = dict(original_cache)

    session_consumer = consumers.AcquisitionConsumer()
    session_consumer.scope = {
        "url_route": {"kwargs": {"session_id": str(session.id)}}
    }
    assert "user" not in session_consumer.scope
    assert contract["transport"]["consumer_authorization_enforced"] is False
    session_consumer.channel_layer = _ChannelLayer()
    session_consumer.channel_name = "contract-session-channel"
    session_frames: list[dict] = []

    async def accept_session() -> None:
        return None

    async def send_session(*, text_data: str) -> None:
        session_frames.append(json.loads(text_data))

    session_consumer.accept = accept_session
    session_consumer.send = send_session
    with monkeypatch.context() as cache_patch:
        isolated_cache: dict = {}
        cache_patch.setattr(consumers, "_status_cache", isolated_cache)
        async_to_sync(session_consumer.connect)()
        assert isolated_cache
    assert consumers._status_cache is original_cache
    assert original_cache == original_cache_snapshot

    session_rule = contract["paths"][0]["on_connect"]
    assert len(session_frames) == 1
    session_frame = session_frames[0]
    assert sorted(session_frame) == session_rule["frame_keys"]
    assert session_frame["type"] == session_rule["type"]
    assert isinstance(session_frame[session_rule["payload_key"]], dict)
    assert sorted(session_frame[session_rule["payload_key"]]) == session_rule[
        "payload_keys"
    ]
    for key in ("event_id", "sequence", "resume_cursor"):
        assert key not in session_frame

    acquisition_models.AcquisitionSession.objects.bulk_create([
        acquisition_models.AcquisitionSession(
            task=task,
            status=acquisition_models.AcquisitionSession.STATUS_RUNNING,
            started_at=timezone.now(),
        )
        for _ in range(50)
    ])
    global_consumer = consumers.GlobalAcquisitionConsumer()
    global_consumer.scope = {}
    assert "user" not in global_consumer.scope
    global_consumer.channel_layer = _ChannelLayer()
    global_consumer.channel_name = "contract-global-channel"
    global_frames: list[dict] = []

    async def accept_global() -> None:
        return None

    async def send_global(*, text_data: str) -> None:
        global_frames.append(json.loads(text_data))

    global_consumer.accept = accept_global
    global_consumer.send = send_global
    async_to_sync(global_consumer.connect)()

    global_rule = contract["paths"][1]["on_connect"]
    assert len(global_frames) == 1
    global_frame = global_frames[0]
    assert sorted(global_frame) == global_rule["frame_keys"]
    assert global_frame["type"] == global_rule["type"]
    rows = global_frame[global_rule["payload_key"]]
    assert isinstance(rows, list)
    assert len(rows) == global_rule["maximum_sessions"]
    assert all(sorted(row) == global_rule["item_keys"] for row in rows)
    for key in ("event_id", "sequence", "resume_cursor"):
        assert key not in global_frame


def test_client_frames_are_ignored_and_replay_is_absent() -> None:
    contract = load_contract("websocket-v1.json")
    assert contract["transport"]["replay"] is False
    assert contract["transport"]["resume_cursor"] is False
    classes = (consumers.AcquisitionConsumer, consumers.GlobalAcquisitionConsumer)
    for path_rule, consumer_class in zip(contract["paths"], classes, strict=True):
        recorder = _Recorder()
        asyncio.run(
            consumer_class.receive(
                recorder,
                text_data=json.dumps({"resume_cursor": "synthetic-cursor"}),
            )
        )
        assert path_rule["client_to_server"]["behavior"] == "ignored"
        assert len(recorder.messages) == path_rule["client_to_server"]["reply_frames"]


def test_data_batch_is_built_by_production_websocket_sink() -> None:
    contract = load_contract("websocket-v1.json")["data_batch"]
    batch_count_rule = contract["batch_count_rule"]
    session_id = 73
    sink = WebSocketSink(SimpleNamespace(id=session_id), broadcast_interval=60)
    sink._stop.set()
    sink._thread.join(timeout=1)
    layer = _ChannelLayer()
    sink._channel_layer = layer
    raw_readings = [
        Reading(
            point_code=f"synthetic-point-{index:03d}",
            value=index,
            timestamp_ns=(index + 1) * 1_000_000_000,
            quality="good",
        )
        for index in range(201)
    ]
    # The latest duplicate must replace point 000's value without changing its
    # insertion position. 202 raw readings therefore become 201 unique points,
    # forcing the production 200-reading frame cap to emit two chunks.
    raw_readings.append(
        Reading(
            point_code="synthetic-point-000",
            value=9999,
            timestamp_ns=999_000_000_000,
            quality="good",
        )
    )
    assert len(raw_readings) == 202
    expected_point_codes = {f"synthetic-point-{index:03d}" for index in range(201)}
    assert {reading.point_code for reading in raw_readings} == expected_point_codes
    with sink._lock:
        sink._buffer = raw_readings

    sink._broadcast_once()

    expected_groups = [
        group.replace("{session_id}", str(session_id))
        for group in contract["groups"]
    ]
    assert [group for group, _message in layer.sent] == [
        expected_groups[0],
        expected_groups[1],
        expected_groups[0],
        expected_groups[1],
    ]

    for group in expected_groups:
        envelopes = [message for target, message in layer.sent if target == group]
        assert len(envelopes) == 2
        assert all(sorted(envelope) == ["data", "type"] for envelope in envelopes)
        assert all(envelope["type"] == contract["event_type"] for envelope in envelopes)

        payloads = [envelope[contract["payload_key"]] for envelope in envelopes]
        assert all(sorted(payload) == contract["keys"] for payload in payloads)
        assert batch_count_rule["present_on_every_chunk"] is True
        assert all("batch_count" in payload for payload in payloads)
        assert [payload["chunk_index"] for payload in payloads] == [0, 1]
        assert [payload["chunk_count"] for payload in payloads] == [2, 2]
        batch_counts = [payload["batch_count"] for payload in payloads]
        assert batch_count_rule["first_chunk_source"] == (
            "raw_reading_count_before_latest_per_point_deduplication"
        )
        assert batch_counts == [
            len(raw_readings),
            batch_count_rule["later_chunk_value"],
        ]
        assert batch_counts == [202, 0]
        assert batch_counts[0] != len(expected_point_codes)
        # A non-zero later chunk would double-count one broadcast cycle in the
        # browser. These assertions intentionally fail if that legacy wire
        # semantic is changed to repeat the raw total on every frame.
        assert batch_counts[1:] == [batch_count_rule["later_chunk_value"]] * (
            len(payloads) - 1
        )
        assert sum(batch_counts) == len(raw_readings)
        assert all(payload["session_id"] == session_id for payload in payloads)
        assert all(len(payload["readings"]) <= 200 for payload in payloads)
        assert [len(payload["readings"]) for payload in payloads] == [200, 1]

        readings = [
            reading
            for payload in payloads
            for reading in payload["readings"]
        ]
        assert all(sorted(reading) == contract["reading_keys"] for reading in readings)
        point_codes = [reading["point_code"] for reading in readings]
        assert len(point_codes) == 201
        assert len(set(point_codes)) == 201
        assert set(point_codes) == expected_point_codes
        assert next(
            reading["value"]
            for reading in readings
            if reading["point_code"] == "synthetic-point-000"
        ) == 9999
        for envelope in envelopes:
            for key in ("event_id", "sequence", "resume_cursor"):
                assert key not in envelope


def test_browser_reconnect_and_resync_rules_are_explicitly_pinned() -> None:
    contract = load_contract("websocket-v1.json")["browser_policy"]
    hook = (REPOSITORY_ROOT / "frontend/src/hooks/useWebSocket.ts").read_text(encoding="utf-8")
    panel = (REPOSITORY_ROOT / "frontend/src/components/acquisition/TaskControlPanel.tsx").read_text(encoding="utf-8")
    page = (REPOSITORY_ROOT / "frontend/src/pages/AcquisitionControlPage.tsx").read_text(encoding="utf-8")

    assert f"autoReconnect = {str(contract['hook_default_auto_reconnect']).lower()}" in hook
    assert f"reconnectInterval = {contract['fixed_delay_ms']}" in hook
    assert "}, reconnectIntervalRef.current);" in hook
    assert contract["backoff"] is False
    assert contract["jitter"] is False
    assert contract["maximum_attempts"] is None
    assert "Math.random(" not in hook
    assert "reconnectattempt" not in hook.lower()
    global_call = page.split("const { status: wsStatus } = useWebSocket({", 1)[1].split(
        "});", 1
    )[0]
    assert contract["global_socket_auto_reconnect"] is True
    assert "autoReconnect" not in global_call
    assert f"autoReconnect: {str(contract['session_panel_auto_reconnect']).lower()}" in panel
    assert f"setInterval(poll, {contract['rest_resync_poll_ms']})" in page
