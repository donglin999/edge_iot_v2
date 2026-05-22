"""Agent-level offline degradation + reconnect backfill tests (XIU-72, M5).

These exercise the :class:`EdgeAgent` glue around the durable outbox:

- ``_sync_outbox_to_center`` reconnect reconciliation
- ``_handle_ack`` outbox pruning from center acks
- ``_uplink_loop`` draining the outbox and tagging replayed history with
  ``backfill: true`` while shipping live frames un-tagged
"""
from __future__ import annotations

import asyncio
import json

import pytest

from edge_agent.agent import EdgeAgent
from edge_agent.config import EdgeConfig
from edge_agent.outbox import DurableOutbox


def _cfg(**overrides) -> EdgeConfig:
    base = dict(
        edge_id="edge-1", edge_token="tk", center_url="ws://test/ws/fleet/",
        labels={}, log_level="INFO",
    )
    base.update(overrides)
    return EdgeConfig(**base)


def _lifecycle(seq: int) -> dict:
    return {"type": "lifecycle", "monotonic_seq": seq, "event": "session.online"}


class CollectWS:
    """Minimal WS double: records every frame sent, never closes itself."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


@pytest.mark.asyncio
async def test_sync_prunes_delivered_and_arms_backfill(tmp_path):
    ob = DurableOutbox(str(tmp_path / "o.db"))
    for _ in range(5):
        ob.append(_lifecycle)
    agent = EdgeAgent(_cfg(), durable_outbox=ob)
    # Center confirms it already has through seq 3.
    agent._center_last_seq = 3
    await agent._sync_outbox_to_center()

    # Delivered frames pruned; 4 & 5 stay as the backfill set.
    assert [seq for seq, _ in ob.pending()] == [4, 5]
    # Uplink loop will resend from the center's high-water mark.
    assert agent._uplink_sent_high == 3
    # Everything currently buffered is backfill history.
    assert agent._backfill_through == 5


@pytest.mark.asyncio
async def test_handle_ack_prunes_outbox(tmp_path):
    ob = DurableOutbox(str(tmp_path / "o.db"))
    for _ in range(4):
        ob.append(_lifecycle)
    agent = EdgeAgent(_cfg(), durable_outbox=ob)

    await agent._handle_ack({"type": "ack", "last_uplink_seq": 2})
    assert [seq for seq, _ in ob.pending()] == [3, 4]


@pytest.mark.asyncio
async def test_handle_ack_without_seq_is_noop(tmp_path):
    ob = DurableOutbox(str(tmp_path / "o.db"))
    for _ in range(3):
        ob.append(_lifecycle)
    agent = EdgeAgent(_cfg(), durable_outbox=ob)

    await agent._handle_ack({"type": "ack", "ref": "heartbeat"})  # no seq
    assert ob.depth() == 3


@pytest.mark.asyncio
async def test_uplink_loop_backfills_then_ships_live_untagged(tmp_path):
    """The reconnect backfill set ships with ``backfill: true``; frames
    produced live during the session ship exactly as built."""
    ob = DurableOutbox(str(tmp_path / "o.db"))
    # 3 frames buffered while offline.
    for _ in range(3):
        ob.append(_lifecycle)
    agent = EdgeAgent(_cfg(durable_outbox_batch=None) if False else _cfg(),
                      durable_outbox=ob)
    # Reconnect with the center having nothing → all 3 are backfill.
    agent._center_last_seq = 0
    await agent._sync_outbox_to_center()
    assert agent._backfill_through == 3

    ws = CollectWS()
    loop_task = asyncio.create_task(agent._uplink_loop(ws))
    # Let the backfill drain.
    await asyncio.sleep(0.2)
    # Now a live frame is produced mid-session.
    agent._enqueue_uplink(_lifecycle)
    await asyncio.sleep(0.2)
    agent._stop.set()
    agent._uplink_signal.set()
    await asyncio.wait_for(loop_task, timeout=2)

    by_seq = {f["monotonic_seq"]: f for f in ws.sent}
    # Backfilled history (seq 1..3) is tagged.
    assert all(by_seq[s].get("backfill") is True for s in (1, 2, 3))
    # The live frame (seq 4, above the reconnect high-water) is not.
    assert "backfill" not in by_seq[4]
