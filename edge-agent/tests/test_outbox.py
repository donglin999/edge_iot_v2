"""Tests for the M5 durable uplink outbox (XIU-72).

Covers the SQLite-backed :class:`~edge_agent.outbox.DurableOutbox`:

- monotonic seq allocation + frame persistence
- ``pending`` / ``ack`` drain + prune semantics
- ``sync_to_center`` reconnect reconciliation (prune + fast-forward)
- the bounded-buffer overflow policy (drop-oldest + throttled warning)
- cross-restart durability (seq counter + unacked frames survive re-open)
"""
from __future__ import annotations

import json

import pytest

from edge_agent.outbox import DurableOutbox


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "edge_state.db")


def _frame(seq: int) -> dict:
    """A minimal uplink-shaped frame stamped with its seq."""
    return {"type": "lifecycle", "monotonic_seq": seq, "event": "session.online"}


# ---------------------------------------------------------------------------
# seq allocation + append
# ---------------------------------------------------------------------------


class TestAppend:
    def test_append_allocates_monotonic_seq(self, db_path):
        ob = DurableOutbox(db_path)
        f1 = ob.append(_frame)
        f2 = ob.append(_frame)
        f3 = ob.append(_frame)
        assert [f1["monotonic_seq"], f2["monotonic_seq"], f3["monotonic_seq"]] == [1, 2, 3]
        assert ob.seq_high() == 3
        assert ob.depth() == 3

    def test_append_returns_built_frame(self, db_path):
        ob = DurableOutbox(db_path)
        frame = ob.append(lambda seq: {"type": "x", "monotonic_seq": seq, "k": "v"})
        assert frame == {"type": "x", "monotonic_seq": 1, "k": "v"}

    def test_fresh_outbox_is_empty(self, db_path):
        ob = DurableOutbox(db_path)
        assert ob.depth() == 0
        assert ob.seq_high() == 0
        assert ob.pending() == []


# ---------------------------------------------------------------------------
# pending / ack
# ---------------------------------------------------------------------------


class TestPendingAndAck:
    def test_pending_returns_frames_in_seq_order(self, db_path):
        ob = DurableOutbox(db_path)
        for _ in range(5):
            ob.append(_frame)
        rows = ob.pending()
        assert [seq for seq, _ in rows] == [1, 2, 3, 4, 5]

    def test_pending_after_skips_already_sent(self, db_path):
        ob = DurableOutbox(db_path)
        for _ in range(5):
            ob.append(_frame)
        rows = ob.pending(after=3)
        assert [seq for seq, _ in rows] == [4, 5]

    def test_pending_limit_bounds_the_batch(self, db_path):
        ob = DurableOutbox(db_path)
        for _ in range(10):
            ob.append(_frame)
        rows = ob.pending(limit=4)
        assert [seq for seq, _ in rows] == [1, 2, 3, 4]

    def test_ack_prunes_through_seq(self, db_path):
        ob = DurableOutbox(db_path)
        for _ in range(5):
            ob.append(_frame)
        dropped = ob.ack(3)
        assert dropped == 3
        assert [seq for seq, _ in ob.pending()] == [4, 5]
        # seq counter is NOT rewound by an ack — only rows are pruned.
        assert ob.seq_high() == 5

    def test_ack_is_idempotent(self, db_path):
        ob = DurableOutbox(db_path)
        for _ in range(3):
            ob.append(_frame)
        assert ob.ack(2) == 2
        assert ob.ack(2) == 0  # re-acking the same seq drops nothing
        assert ob.depth() == 1

    def test_ack_zero_or_negative_is_noop(self, db_path):
        ob = DurableOutbox(db_path)
        ob.append(_frame)
        assert ob.ack(0) == 0
        assert ob.depth() == 1


# ---------------------------------------------------------------------------
# sync_to_center — reconnect reconciliation
# ---------------------------------------------------------------------------


class TestSyncToCenter:
    def test_sync_prunes_frames_center_already_has(self, db_path):
        ob = DurableOutbox(db_path)
        for _ in range(6):
            ob.append(_frame)
        # Center confirms it has through seq 4 → only 5,6 remain to backfill.
        pending = ob.sync_to_center(4)
        assert pending == 2
        assert [seq for seq, _ in ob.pending()] == [5, 6]

    def test_sync_with_zero_keeps_everything(self, db_path):
        ob = DurableOutbox(db_path)
        for _ in range(3):
            ob.append(_frame)
        assert ob.sync_to_center(0) == 3
        assert ob.depth() == 3

    def test_sync_fast_forwards_counter_when_center_ahead(self, db_path):
        """An edge whose state DB was wiped: center seq is ahead of ours."""
        ob = DurableOutbox(db_path)
        ob.append(_frame)  # local high-water = 1
        # Center says it already has seq 100 (this edge ran before its DB
        # was replaced). The counter must jump so the next frame is seq
        # 101 — classified ``advanced`` by the center, not a stale dup.
        ob.sync_to_center(100)
        assert ob.seq_high() == 100
        nxt = ob.append(_frame)
        assert nxt["monotonic_seq"] == 101


# ---------------------------------------------------------------------------
# bounded buffer — overflow policy
# ---------------------------------------------------------------------------


class TestOverflowPolicy:
    def test_cap_drops_oldest_frames(self, db_path):
        ob = DurableOutbox(db_path, max_rows=5)
        for _ in range(8):
            ob.append(_frame)
        # Only the newest 5 survive; the 3 oldest were evicted.
        assert ob.depth() == 5
        assert [seq for seq, _ in ob.pending()] == [4, 5, 6, 7, 8]

    def test_cap_does_not_rewind_seq_counter(self, db_path):
        ob = DurableOutbox(db_path, max_rows=3)
        for _ in range(10):
            ob.append(_frame)
        # Frames were dropped, but the seq keeps climbing — a gap on the
        # center side is the correct, detectable signal of real data loss.
        assert ob.seq_high() == 10
        assert ob.dropped_total() == 7

    def test_process_survives_sustained_overflow(self, db_path):
        """Append far past the cap — must stay bounded, never raise."""
        ob = DurableOutbox(db_path, max_rows=10)
        for _ in range(500):
            ob.append(_frame)
        assert ob.depth() == 10
        assert ob.dropped_total() == 490


# ---------------------------------------------------------------------------
# cross-restart durability
# ---------------------------------------------------------------------------


class TestDurability:
    def test_seq_counter_survives_reopen(self, db_path):
        ob = DurableOutbox(db_path)
        for _ in range(7):
            ob.append(_frame)
        ob.ack(7)  # all delivered + pruned — table empty
        assert ob.depth() == 0
        # Simulate an edge-agent restart: a brand-new instance on the same
        # file. The seq counter must NOT reset to 0 (that is the M3
        # short-session-restart gap this milestone closes).
        reopened = DurableOutbox(db_path)
        assert reopened.seq_high() == 7
        nxt = reopened.append(_frame)
        assert nxt["monotonic_seq"] == 8

    def test_unacked_frames_survive_reopen(self, db_path):
        ob = DurableOutbox(db_path)
        for _ in range(4):
            ob.append(_frame)
        ob.ack(2)  # 1,2 delivered; 3,4 still pending
        reopened = DurableOutbox(db_path)
        rows = reopened.pending()
        assert [seq for seq, _ in rows] == [3, 4]
        # The persisted frame body round-trips intact.
        assert rows[0][1]["event"] == "session.online"

    def test_reopen_recovers_seq_from_leftover_rows(self, db_path):
        """If the counter row lagged a crash, MAX(seq) still wins."""
        ob = DurableOutbox(db_path)
        for _ in range(5):
            ob.append(_frame)
        reopened = DurableOutbox(db_path)
        # Never hands out a seq at or below an existing row.
        assert reopened.seq_high() >= 5
        assert reopened.append(_frame)["monotonic_seq"] == 6
