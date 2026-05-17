"""Reliability tests for InfluxDB throughput & write durability (XIU-3).

Covers:

* **M4** — :class:`storage.spill_queue.SpillQueue` durability and the
  failure -> spill -> replay loop on :class:`storage.influxdb.InfluxDBStorage`,
  including the circuit breaker exposed via ``is_available()``.
* **C2** — the storage is configured for asynchronous *batching* writes.

These tests need no running InfluxDB: the network layer is faked so we can
deterministically simulate an outage and a recovery.
"""
from __future__ import annotations

import pytest

from storage.influxdb import InfluxDBStorage
from storage.spill_queue import SpillQueue


# Two distinct line-protocol batches used across the outage scenarios.
_BATCH_A = "m,site=s1,device=d1 v=1.0 1700000000000000000"
_BATCH_B = "\n".join(
    f"m,site=s1,device=d1 v={i}.0 17000000000000000{i:02d}" for i in range(5)
)


class _FakeReplayApi:
    """Stand-in for a SYNCHRONOUS write_api used by spill replay.

    ``fail_times`` write calls raise (simulating a still-down backend); every
    subsequent call records the payload and succeeds.
    """

    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.writes: list[str] = []

    def write(self, bucket=None, org=None, record=None):  # noqa: D401
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("simulated InfluxDB outage")
        self.writes.append(record)


# ---------------------------------------------------------------------------
# SpillQueue
# ---------------------------------------------------------------------------


class TestSpillQueue:
    def test_push_peek_delete_roundtrip(self, tmp_path):
        q = SpillQueue(str(tmp_path / "spill.sqlite3"))
        q.push(_BATCH_A, points=1)
        q.push(_BATCH_B, points=5)

        assert q.count() == 2
        assert q.pending_points() == 6

        rows = q.peek_batch(10)
        assert [r[1] for r in rows] == [_BATCH_A, _BATCH_B]  # FIFO order

        # peek does not consume — the rows are still there.
        assert q.count() == 2

        q.delete([rows[0][0]])
        assert q.count() == 1
        assert q.peek_batch(10)[0][1] == _BATCH_B

    def test_durable_across_reopen(self, tmp_path):
        """A spilled batch survives a process restart (new SpillQueue)."""
        path = str(tmp_path / "spill.sqlite3")
        SpillQueue(path).push(_BATCH_A, points=1)

        reopened = SpillQueue(path)
        assert reopened.count() == 1
        assert reopened.peek_batch(1)[0][1] == _BATCH_A

    def test_row_cap_drops_oldest(self, tmp_path):
        q = SpillQueue(str(tmp_path / "spill.sqlite3"), max_rows=3)
        for i in range(6):
            q.push(f"m v={i}", points=1)
        # Only the 3 most recent rows are retained.
        assert q.count() == 3
        assert [r[1] for r in q.peek_batch(10)] == ["m v=3", "m v=4", "m v=5"]


# ---------------------------------------------------------------------------
# InfluxDBStorage failure path (M4)
# ---------------------------------------------------------------------------


def _make_storage(tmp_path, **overrides):
    cfg = {
        "url": "http://localhost:8086",
        "token": "t",
        "org": "o",
        "bucket": "b",
        "spill_db_path": str(tmp_path / "influx_spill.sqlite3"),
        "circuit_failure_threshold": 3,
        "circuit_cooldown": 30.0,
        "batch_size": 500,
    }
    cfg.update(overrides)
    return InfluxDBStorage(cfg)


class TestInfluxDBFailurePath:
    def test_batching_config_defaults(self, tmp_path):
        """C2: storage defaults to large-batch async write tuning."""
        s = _make_storage(tmp_path)
        assert s.batch_size == 500
        assert s.flush_interval == 1000
        assert s.jitter_interval == 200

    def test_error_callback_spills_failed_batch(self, tmp_path):
        s = _make_storage(tmp_path)
        s._on_write_error(("b", "o", "ns"), _BATCH_A, RuntimeError("down"))
        s._on_write_error(("b", "o", "ns"), _BATCH_B, RuntimeError("down"))

        assert s._spill.count() == 2
        # _BATCH_B carries 5 newline-separated points.
        assert s._spill.pending_points() == 6
        assert s._consecutive_failures == 2

    def test_circuit_breaker_via_is_available(self, tmp_path):
        s = _make_storage(tmp_path, circuit_failure_threshold=3)
        s.is_connected = True
        assert s.is_available() is True

        for _ in range(3):
            s._on_write_error(("b", "o", "ns"), _BATCH_A, RuntimeError("down"))
        # Circuit open after threshold consecutive failures.
        assert s.is_available() is False

        # A success callback closes the circuit again.
        s._on_write_success(("b", "o", "ns"), _BATCH_A)
        assert s._consecutive_failures == 0
        assert s.is_available() is True

    def test_circuit_half_open_after_cooldown(self, tmp_path):
        s = _make_storage(tmp_path, circuit_failure_threshold=2, circuit_cooldown=30.0)
        s.is_connected = True
        for _ in range(2):
            s._on_write_error(("b", "o", "ns"), _BATCH_A, RuntimeError("down"))
        assert s.is_available() is False
        # Pretend the cooldown has elapsed -> half-open probe is allowed.
        s._last_failure_time -= 31.0
        assert s.is_available() is True


# ---------------------------------------------------------------------------
# Outage -> recovery -> full replay (acceptance criterion #2)
# ---------------------------------------------------------------------------


class TestOutageRecovery:
    def test_spilled_data_fully_replayed_on_recovery(self, tmp_path):
        """Simulate a network outage, then recovery, and assert every spilled
        point is replayed into InfluxDB with no loss."""
        s = _make_storage(tmp_path)

        # --- outage: 4 batches fail and spill to disk -----------------------
        spilled = [
            "m,site=s1 v=1 1700000000000000001",
            "m,site=s1 v=2 1700000000000000002",
            "m,site=s1 v=3 1700000000000000003",
            "m,site=s1 v=4 1700000000000000004",
        ]
        for lp in spilled:
            s._on_write_error(("b", "o", "ns"), lp, RuntimeError("network down"))
        assert s._spill.count() == 4

        # --- recovery: backend healthy again --------------------------------
        s.is_connected = True
        s._consecutive_failures = 0  # a success callback would have cleared it
        fake = _FakeReplayApi()
        s.replay_api = fake

        s._drain_spill_once()

        # Every spilled point was re-written, queue fully drained.
        assert s._spill.count() == 0
        replayed_lines = "\n".join(fake.writes).split("\n")
        assert sorted(replayed_lines) == sorted(spilled)

    def test_replay_retains_rows_when_backend_still_down(self, tmp_path):
        """If the replay write itself fails, the spilled rows are kept."""
        s = _make_storage(tmp_path)
        s._on_write_error(("b", "o", "ns"), _BATCH_A, RuntimeError("down"))
        assert s._spill.count() == 1

        s.is_connected = True
        s._consecutive_failures = 0
        s.replay_api = _FakeReplayApi(fail_times=1)  # outage not over yet

        s._drain_spill_once()

        # Replay failed -> nothing deleted, data still safe on disk.
        assert s._spill.count() == 1
        assert s._consecutive_failures == 1
