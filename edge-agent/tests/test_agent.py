"""End-to-end agent session against a fake WS transport.

We don't bind a real socket — instead `FakeWebSocket` lets each test
script the server side of the protocol while exercising the real
agent code path (register → heartbeat → reader loop).
"""
from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any

import pytest

from edge_agent.agent import EdgeAgent
from edge_agent.backoff import ExponentialBackoff
from edge_agent.config import EdgeConfig
from edge_agent.outbox import DurableOutbox
from edge_agent.protocol import FRAME_ACK, PROTOCOL_VERSION


class FakeWebSocket:
    """Scriptable in-memory WS: ``incoming`` are server→client frames."""

    def __init__(self, incoming: list[dict] | None = None) -> None:
        self._incoming = deque(incoming or [])
        self.sent: list[dict] = []
        self._closed = asyncio.Event()
        self._next_event = asyncio.Event()
        if incoming:
            self._next_event.set()

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        while not self._incoming:
            if self._closed.is_set():
                from websockets.exceptions import ConnectionClosed
                raise ConnectionClosed(None, None)
            self._next_event.clear()
            await self._next_event.wait()
        return json.dumps(self._incoming.popleft())

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        while not self._incoming:
            if self._closed.is_set():
                raise StopAsyncIteration
            self._next_event.clear()
            await self._next_event.wait()
        return json.dumps(self._incoming.popleft())

    async def close(self) -> None:
        self._closed.set()
        self._next_event.set()

    def push(self, frame: dict) -> None:
        self._incoming.append(frame)
        self._next_event.set()


def _cfg(**overrides: Any) -> EdgeConfig:
    base = dict(
        edge_id="edge-1", edge_token="tk", center_url="ws://test/ws/fleet/",
        labels={}, log_level="INFO",
    )
    base.update(overrides)
    return EdgeConfig(**base)


@pytest.mark.asyncio
async def test_register_then_heartbeat_sends_expected_frames(tmp_path):
    fake = FakeWebSocket(incoming=[
        {"v": PROTOCOL_VERSION, "type": FRAME_ACK, "ref": "register"},
        # then a bunch of heartbeat acks
        {"v": PROTOCOL_VERSION, "type": FRAME_ACK, "ref": "heartbeat"},
        {"v": PROTOCOL_VERSION, "type": FRAME_ACK, "ref": "heartbeat"},
    ])

    async def factory(url: str):
        assert url == "ws://test/ws/fleet/"
        return fake

    agent = EdgeAgent(
        _cfg(), heartbeat_interval=0.05, connect_factory=factory,
        durable_outbox=DurableOutbox(str(tmp_path / "outbox.db")),
    )

    task = asyncio.create_task(agent.run())
    # Let the agent send register + at least 2 heartbeats.
    await asyncio.sleep(0.25)
    agent.stop()
    await fake.close()
    await asyncio.wait_for(task, timeout=2)

    types = [f["type"] for f in fake.sent]
    assert types[0] == "register"
    assert types.count("heartbeat") >= 2
    assert fake.sent[0]["edge_id"] == "edge-1"
    assert fake.sent[0]["token"] == "tk"
    # v0.3: a session.online lifecycle frame is emitted right after register,
    # carrying the first uplink monotonic_seq.
    online = next(f for f in fake.sent if f["type"] == "lifecycle")
    assert online["event"] == "session.online"
    assert online["monotonic_seq"] == 1
    for hb in (f for f in fake.sent[1:] if f["type"] == "heartbeat"):
        assert hb["edge_id"] == "edge-1"
        assert hb["uptime"] >= 0


@pytest.mark.asyncio
async def test_reconnect_uses_backoff_and_resets_on_success(tmp_path):
    """Failed connects should bump the backoff; a successful register resets it.

    This is the spec'd guarantee from docs/distributed/protocol.md:
    "the backoff timer is reset to 1s after the next successful register ack".
    """
    attempts = []

    async def factory(url: str):
        attempts.append(len(attempts))
        # First two attempts fail outright.
        if len(attempts) <= 2:
            raise OSError("conn refused")
        # Third attempt succeeds; respond with ack to register then close.
        fake = FakeWebSocket(incoming=[
            {"v": PROTOCOL_VERSION, "type": FRAME_ACK, "ref": "register"},
        ])
        # Auto-close after a tiny delay so _run_session exits cleanly.
        async def autoclose():
            await asyncio.sleep(0.05)
            await fake.close()
        asyncio.create_task(autoclose())
        return fake

    sleeps: list[float] = []
    backoff = ExponentialBackoff(initial=0.01, factor=2.0, cap=0.16)

    agent = EdgeAgent(
        _cfg(), heartbeat_interval=10, connect_factory=factory, backoff=backoff,
        durable_outbox=DurableOutbox(str(tmp_path / "outbox.db")),
    )
    # Patch the sleep helper so we can assert the schedule without
    # actually waiting seconds in the test.
    orig_sleep = agent._sleep

    async def trace_sleep(seconds: float):
        sleeps.append(seconds)
        await asyncio.sleep(0)  # yield without delay

    agent._sleep = trace_sleep  # type: ignore[assignment]

    task = asyncio.create_task(agent.run())
    await asyncio.sleep(0.3)
    agent.stop()
    await asyncio.wait_for(task, timeout=2)

    # First two sleeps are the failed-connect backoffs: 0.01, 0.02.
    assert sleeps[0] == pytest.approx(0.01)
    assert sleeps[1] == pytest.approx(0.02)
    # The third sleep happens AFTER the successful session ended, and the
    # backoff should have been reset by `_run_session` → back to 0.01.
    assert sleeps[2] == pytest.approx(0.01)
