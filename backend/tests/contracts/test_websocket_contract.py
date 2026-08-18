"""WebSocket route, frame and browser reconnect compatibility contract."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from django.conf import settings

from acquisition import consumers, routing

from .helpers import REPOSITORY_ROOT, load_contract


class _Recorder:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send(self, *, text_data: str) -> None:
        self.messages.append(json.loads(text_data))


def _dispatch(method, event: dict) -> dict:
    recorder = _Recorder()
    asyncio.run(method(recorder, event))
    assert len(recorder.messages) == 1
    return recorder.messages[0]


def test_websocket_routes_and_channel_delivery_window() -> None:
    contract = load_contract("websocket-v1.json")
    patterns = [str(pattern.pattern) for pattern in routing.websocket_urlpatterns]
    assert patterns == [item["django_regex"] for item in contract["paths"]]

    layer = settings.CHANNEL_LAYERS["default"]["CONFIG"]
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

    for variant in contract["forwarded_events"]:
        method = getattr(classes[variant["consumer"]], variant["handler"])
        event = {"data": payload, "event": "created", "alarm": alarm}
        frame = _dispatch(method, event)
        assert frame == variant["example_frame"]


def test_browser_reconnect_and_resync_rules_are_explicitly_pinned() -> None:
    contract = load_contract("websocket-v1.json")["browser_policy"]
    hook = (REPOSITORY_ROOT / "frontend/src/hooks/useWebSocket.ts").read_text(encoding="utf-8")
    panel = (REPOSITORY_ROOT / "frontend/src/components/acquisition/TaskControlPanel.tsx").read_text(encoding="utf-8")
    page = (REPOSITORY_ROOT / "frontend/src/pages/AcquisitionControlPage.tsx").read_text(encoding="utf-8")

    assert f"autoReconnect = {str(contract['hook_default_auto_reconnect']).lower()}" in hook
    assert f"reconnectInterval = {contract['fixed_delay_ms']}" in hook
    assert "autoReconnect: false" in panel
    assert f"setInterval(poll, {contract['rest_resync_poll_ms']})" in page
