"""Durable, cross-restart uplink outbox (M5 — offline degradation + backfill).

M3/M4 buffered ``lifecycle`` / ``sample_batch`` / ``alarm_event`` frames in a
process-local ``queue.Queue``: anything not yet flushed to the center was
lost on an edge-agent restart. M5 replaces that with this SQLite-backed
outbox so an edge can be offline for an hour — and restarted in the middle
of it — without dropping a single uplink event.

Design
------
* Every uplink frame is assigned a strictly-increasing ``monotonic_seq``
  and persisted *before* it is ever put on the wire. The seq counter lives
  in a meta row, so it keeps climbing even after acked rows are pruned and
  across process restarts (this is the change from M3's per-process
  counter — see ``docs/distributed/protocol.md`` § Uplink sequencing).
* On reconnect the center reports its high-water ``last_uplink_seq`` in the
  ``register`` ack. :meth:`sync_to_center` prunes everything the center
  already has and leaves exactly the gap frames for the agent to backfill.
* The center acks each accepted uplink frame with its new
  ``last_uplink_seq``; :meth:`ack` prunes the outbox up to that point so a
  long-lived online session keeps the table small.

This module is plain ``sqlite3`` (no Django ORM) on the same ``edge_state.db``
file the rest of the edge uses — KV-store style, exactly like
:class:`edge_agent.state.EdgeStateStore`. It is safe to call from the
acquisition worker threads and the asyncio loop thread concurrently: a
process-local lock plus per-call connections serialise writers.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


_OUTBOX_DDL = """
CREATE TABLE IF NOT EXISTS edge_uplink_outbox (
    seq        INTEGER PRIMARY KEY,
    frame      TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edge_uplink_meta (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""

# Meta key holding the highest ``monotonic_seq`` ever allocated. Survives
# row pruning and process restarts — this is what makes the edge seq
# persistent (M5) instead of M3's per-process counter.
_KEY_SEQ_HIGH = "seq_high"

# Safety cap on the outbox depth. The M5 acceptance scenario (1 h offline,
# ~1 Hz uplink) is ~3.6k rows; the default leaves headroom for a multi-hour
# outage while staying bounded so the edge can never OOM. When the cap
# trips we drop the *oldest* unacked frames — those uplink events are lost.
_DEFAULT_MAX_ROWS = 10_000

# Overflow is logged at most once per this interval (throttled) so a long
# saturated outage does not spew one ERROR line per dropped frame.
_OVERFLOW_LOG_INTERVAL_S = 60.0

# Build callback: given the freshly-allocated seq, return the wire frame.
FrameBuilder = Callable[[int], Dict[str, Any]]


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class DurableOutbox:
    """SQLite-backed, restart-surviving buffer of pending uplink frames."""

    def __init__(self, db_path: str | Path, *, max_rows: int = _DEFAULT_MAX_ROWS) -> None:
        self.db_path = str(db_path)
        self.max_rows = int(max_rows)
        # Serialises seq allocation + writes across the acquisition worker
        # threads and the asyncio loop thread (process-local; sqlite's own
        # busy-timeout covers the cross-process Django ORM connection).
        self._lock = threading.Lock()
        # Overflow accounting — cumulative dropped count + throttle clock
        # for the (otherwise spammy) "buffer full" warning.
        self._dropped_total = 0
        self._last_overflow_log = 0.0
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.executescript(_OUTBOX_DDL)
            conn.execute(
                "INSERT OR IGNORE INTO edge_uplink_meta(key, value) VALUES (?, 0)",
                (_KEY_SEQ_HIGH,),
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=30.0)

    # ---- producer ---------------------------------------------------------

    def append(self, build_frame: FrameBuilder) -> Dict[str, Any]:
        """Allocate the next seq, build the frame, persist it, return it.

        The seq increment, frame build and row insert all happen under one
        ``BEGIN IMMEDIATE`` transaction + the process lock, so concurrent
        producers (task runner + WebSocketSink + AlarmSink threads) get
        strictly-ordered, gap-free sequence numbers.
        """
        with self._lock, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT value FROM edge_uplink_meta WHERE key = ?",
                    (_KEY_SEQ_HIGH,),
                ).fetchone()
                seq = int(row[0]) + 1
                frame = build_frame(seq)
                conn.execute(
                    "UPDATE edge_uplink_meta SET value = ? WHERE key = ?",
                    (seq, _KEY_SEQ_HIGH),
                )
                conn.execute(
                    "INSERT INTO edge_uplink_outbox(seq, frame, created_at) "
                    "VALUES (?, ?, ?)",
                    (seq, json.dumps(frame, ensure_ascii=False), _utc_now_iso()),
                )
                self._enforce_cap(conn)
                conn.execute("COMMIT")
                return frame
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def _enforce_cap(self, conn: sqlite3.Connection) -> None:
        """Drop the oldest rows if the outbox has grown past ``max_rows``."""
        (depth,) = conn.execute(
            "SELECT COUNT(*) FROM edge_uplink_outbox"
        ).fetchone()
        if depth <= self.max_rows:
            return
        excess = depth - self.max_rows
        conn.execute(
            "DELETE FROM edge_uplink_outbox WHERE seq IN "
            "(SELECT seq FROM edge_uplink_outbox ORDER BY seq LIMIT ?)",
            (excess,),
        )
        # Throttled warning: a long saturated outage would otherwise log
        # one line per appended frame. Accumulate the count and emit at
        # most once per _OVERFLOW_LOG_INTERVAL_S.
        self._dropped_total += excess
        now = time.monotonic()
        if now - self._last_overflow_log >= _OVERFLOW_LOG_INTERVAL_S:
            self._last_overflow_log = now
            logger.warning(
                "DurableOutbox: buffer full at cap %d — dropped %d oldest "
                "frame(s) just now, %d total since start; those uplink "
                "events are lost",
                self.max_rows, excess, self._dropped_total,
            )

    # ---- consumer ---------------------------------------------------------

    def pending(self, *, after: int = 0, limit: Optional[int] = None) -> List[Tuple[int, Dict[str, Any]]]:
        """Return ``(seq, frame)`` rows with ``seq > after``, oldest first.

        ``after`` lets the sender skip frames it has already pushed on the
        current session without pruning them (they stay until acked, so a
        mid-session disconnect resends them). ``limit`` bounds the batch so
        a large backfill can be throttled.
        """
        sql = "SELECT seq, frame FROM edge_uplink_outbox WHERE seq > ? ORDER BY seq"
        params: List[Any] = [int(after)]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        out: List[Tuple[int, Dict[str, Any]]] = []
        for seq, raw in rows:
            try:
                out.append((int(seq), json.loads(raw)))
            except json.JSONDecodeError:
                logger.warning("DurableOutbox: row seq=%s is not JSON — skipping", seq)
        return out

    def ack(self, seq: int) -> int:
        """Prune every row with ``seq <= seq`` (the center confirmed them).

        Returns the number of rows dropped. Idempotent: re-acking an
        already-pruned seq deletes nothing.
        """
        seq = int(seq)
        if seq <= 0:
            return 0
        with self._lock, closing(self._connect()) as conn:
            cur = conn.execute(
                "DELETE FROM edge_uplink_outbox WHERE seq <= ?", (seq,)
            )
            conn.commit()
            return cur.rowcount

    def sync_to_center(self, center_seq: int) -> int:
        """Reconcile the outbox against the center's high-water mark.

        Called once per reconnect with the ``last_uplink_seq`` the center
        reported in its ``register`` ack:

        * Frames with ``seq <= center_seq`` are pruned — the center already
          has them.
        * If the center is *ahead* of our local counter (an edge whose
          ``edge_state.db`` was wiped / replaced), fast-forward the seq
          counter to ``center_seq`` so the next frame is classified
          ``advanced`` rather than a stale ``duplicate``.

        Returns the number of frames still pending (the backfill set).
        """
        center_seq = int(center_seq or 0)
        with self._lock, closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "DELETE FROM edge_uplink_outbox WHERE seq <= ?", (center_seq,)
                )
                (high,) = conn.execute(
                    "SELECT value FROM edge_uplink_meta WHERE key = ?",
                    (_KEY_SEQ_HIGH,),
                ).fetchone()
                if center_seq > int(high):
                    conn.execute(
                        "UPDATE edge_uplink_meta SET value = ? WHERE key = ?",
                        (center_seq, _KEY_SEQ_HIGH),
                    )
                    logger.warning(
                        "DurableOutbox: center seq %d ahead of local %d — "
                        "fast-forwarding counter (edge state DB reset?)",
                        center_seq, high,
                    )
                (depth,) = conn.execute(
                    "SELECT COUNT(*) FROM edge_uplink_outbox"
                ).fetchone()
                conn.execute("COMMIT")
                return int(depth)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    # ---- introspection ----------------------------------------------------

    def seq_high(self) -> int:
        """Highest ``monotonic_seq`` allocated so far (0 on a fresh outbox)."""
        with closing(self._connect()) as conn:
            (high,) = conn.execute(
                "SELECT value FROM edge_uplink_meta WHERE key = ?",
                (_KEY_SEQ_HIGH,),
            ).fetchone()
        return int(high)

    def depth(self) -> int:
        """Number of unacked frames currently buffered."""
        with closing(self._connect()) as conn:
            (depth,) = conn.execute(
                "SELECT COUNT(*) FROM edge_uplink_outbox"
            ).fetchone()
        return int(depth)

    def dropped_total(self) -> int:
        """Frames evicted by the overflow cap since this process started."""
        with self._lock:
            return self._dropped_total
