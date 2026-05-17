"""Durable on-disk spill queue for failed InfluxDB writes.

Backs requirement **M4** (失败无退避/不闭环): when a live write to InfluxDB
fails, the failed batch is *spilled* to this SQLite-backed FIFO queue so it
survives both transient network outages and full process restarts. A replay
worker (see :mod:`storage.influxdb`) drains the queue back into InfluxDB once
the backend recovers — guaranteeing no data loss on reconnect.

The queue stores raw InfluxDB *line protocol* strings (one spill row may hold
a whole batch, newline-separated). SQLite is opened in WAL mode so the writer
(error callback) and reader (replay worker) — which may run in different
threads, or even different processes sharing the same file — do not block each
other. A process-local lock serialises this instance's own access; cross-
process safety relies on SQLite's file locking plus a generous busy timeout.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import List, Tuple

logger = logging.getLogger(__name__)

# Hard cap on retained spill rows. One row == one failed batch, so at the
# default InfluxDB batch size of 500 points this is ~100M points of headroom.
# When exceeded we drop the *oldest* rows — recent data is preferred over a
# stale backlog, mirroring the bounded-buffer policy in InfluxDBSink.
_DEFAULT_MAX_ROWS = 200_000


class SpillQueue:
    """SQLite-backed FIFO queue of InfluxDB line-protocol payloads."""

    def __init__(self, db_path: str, max_rows: int = _DEFAULT_MAX_ROWS) -> None:
        self._db_path = str(db_path)
        self._max_rows = int(max_rows)
        self._lock = threading.Lock()
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ------------------------------------------------------------------ setup
    def _connect(self) -> sqlite3.Connection:
        # A fresh connection per operation keeps us free of thread-affinity
        # constraints (sqlite3 connections are not shareable across threads).
        # WAL + a 30 s busy timeout make concurrent access from the error
        # callback and the replay worker safe.
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS spill (
                        id         INTEGER PRIMARY KEY AUTOINCREMENT,
                        payload    TEXT    NOT NULL,
                        points     INTEGER NOT NULL DEFAULT 0,
                        created_at REAL    NOT NULL
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------- public API
    def push(self, payload: str, points: int = 0) -> None:
        """Append a failed line-protocol batch to the tail of the queue."""
        if not payload:
            return
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO spill (payload, points, created_at) VALUES (?, ?, ?)",
                    (payload, int(points), time.time()),
                )
                # Enforce the row cap: delete everything older than the
                # newest _max_rows rows.
                conn.execute(
                    """
                    DELETE FROM spill WHERE id IN (
                        SELECT id FROM spill ORDER BY id DESC LIMIT -1 OFFSET ?
                    )
                    """,
                    (self._max_rows,),
                )
                conn.commit()
            finally:
                conn.close()

    def peek_batch(self, limit: int) -> List[Tuple[int, str]]:
        """Return up to ``limit`` oldest rows as ``(id, payload)`` without
        removing them — callers delete only after a successful replay."""
        if limit <= 0:
            return []
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT id, payload FROM spill ORDER BY id ASC LIMIT ?",
                    (int(limit),),
                )
                return cur.fetchall()
            finally:
                conn.close()

    def delete(self, ids: List[int]) -> None:
        """Remove rows by id — invoked once their payload is durably re-written."""
        if not ids:
            return
        with self._lock:
            conn = self._connect()
            try:
                conn.executemany(
                    "DELETE FROM spill WHERE id = ?", [(int(i),) for i in ids]
                )
                conn.commit()
            finally:
                conn.close()

    def count(self) -> int:
        """Number of spilled batches currently pending replay."""
        with self._lock:
            conn = self._connect()
            try:
                return int(conn.execute("SELECT COUNT(*) FROM spill").fetchone()[0])
            finally:
                conn.close()

    def pending_points(self) -> int:
        """Best-effort total of individual points awaiting replay."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT COALESCE(SUM(points), 0) FROM spill").fetchone()
                return int(row[0] or 0)
            finally:
                conn.close()
