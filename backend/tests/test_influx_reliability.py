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

import multiprocessing
import os
import threading

import pytest

from storage import influxdb as influxdb_module
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


def _drain_shared_spill_in_child(
    spill_path,
    write_started,
    release_write,
    process_done,
    write_count,
):
    """Spawn-safe helper for the POSIX cross-process replay-lock test."""

    class _BlockingReplayApi:
        def write(self, bucket=None, org=None, record=None):
            with write_count.get_lock():
                write_count.value += 1
            write_started.set()
            if not release_write.wait(timeout=10.0):
                raise TimeoutError("test did not release the simulated Influx write")

    try:
        storage = InfluxDBStorage({
            "url": "http://localhost:8086",
            "token": "t",
            "org": "o",
            "bucket": "b",
            "spill_db_path": spill_path,
        })
        storage.is_connected = True
        storage.replay_api = _BlockingReplayApi()
        storage._drain_spill_once()
    finally:
        process_done.set()


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
    def test_environment_selects_spill_path_when_config_omits_it(
        self, tmp_path, monkeypatch,
    ):
        env_path = tmp_path / "persistent" / "spill.sqlite3"
        monkeypatch.setenv("INFLUX_SPILL_DB_PATH", str(env_path))

        s = InfluxDBStorage({"url": "http://localhost:8086"})

        assert s.spill_db_path == os.path.abspath(env_path)
        assert env_path.exists()

    def test_explicit_config_path_wins_over_environment(self, tmp_path, monkeypatch):
        env_path = tmp_path / "from-env.sqlite3"
        config_path = tmp_path / "from-config.sqlite3"
        monkeypatch.setenv("INFLUX_SPILL_DB_PATH", str(env_path))

        s = InfluxDBStorage({
            "url": "http://localhost:8086",
            "spill_db_path": str(config_path),
        })

        assert s.spill_db_path == os.path.abspath(config_path)
        assert config_path.exists()
        assert not env_path.exists()

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

    def test_concurrent_storage_instances_do_not_replay_same_rows(self, tmp_path):
        """One Celery threads worker can host several storage instances.

        They share a spill DB, so replay must serialise the whole
        peek/write/delete sequence rather than relying on SQLite's per-statement
        locking (which would allow both instances to peek the same rows).
        """
        path = tmp_path / "shared-spill.sqlite3"
        first = _make_storage(tmp_path, spill_db_path=str(path))
        second = _make_storage(tmp_path, spill_db_path=str(path))
        first._spill.push(_BATCH_A, points=1)

        write_started = threading.Event()
        release_write = threading.Event()

        class BlockingReplayApi:
            def __init__(self):
                self.writes = []

            def write(self, bucket=None, org=None, record=None):
                self.writes.append(record)
                write_started.set()
                assert release_write.wait(timeout=2.0)

        replay = BlockingReplayApi()
        for storage in (first, second):
            storage.is_connected = True
            storage.replay_api = replay

        first_thread = threading.Thread(target=first._drain_spill_once)
        first_thread.start()
        assert write_started.wait(timeout=2.0)

        second_thread = threading.Thread(target=second._drain_spill_once)
        second_thread.start()
        second_thread.join(timeout=1.0)
        assert not second_thread.is_alive()

        release_write.set()
        first_thread.join(timeout=2.0)
        assert not first_thread.is_alive()

        assert replay.writes == [_BATCH_A]
        assert first._spill.count() == 0

    @pytest.mark.skipif(
        influxdb_module.fcntl is None,
        reason="POSIX fcntl is required for the cross-process replay lease",
    )
    def test_posix_file_lock_prevents_duplicate_replay_across_processes(
        self, tmp_path,
    ):
        """Two independent processes must not replay the same spilled row.

        The first process blocks inside its simulated synchronous Influx write
        while holding the advisory lock.  The second process therefore has
        time to attempt the same drain and must return without entering its
        write call.  ``spawn`` is deliberate: no Python lock object is shared,
        so passing this test proves coordination comes from the lock file.
        """
        ctx = multiprocessing.get_context("spawn")
        path = str(tmp_path / "cross-process-spill.sqlite3")
        SpillQueue(path).push(_BATCH_A, points=1)

        release_write = ctx.Event()
        first_write_started = ctx.Event()
        second_write_started = ctx.Event()
        first_done = ctx.Event()
        second_done = ctx.Event()
        write_count = ctx.Value("i", 0)

        first = ctx.Process(
            target=_drain_shared_spill_in_child,
            args=(
                path,
                first_write_started,
                release_write,
                first_done,
                write_count,
            ),
        )
        second = ctx.Process(
            target=_drain_shared_spill_in_child,
            args=(
                path,
                second_write_started,
                release_write,
                second_done,
                write_count,
            ),
        )

        first_entered_write = False
        second_finished_while_locked = False
        try:
            first.start()
            first_entered_write = first_write_started.wait(timeout=10.0)
            if first_entered_write:
                second.start()
                second_finished_while_locked = second_done.wait(timeout=10.0)
        finally:
            release_write.set()
            for process in (first, second):
                if process.pid is None:
                    continue
                process.join(timeout=10.0)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5.0)

        assert first_entered_write, "first replay process never reached its write"
        assert second_finished_while_locked, (
            "second replay process blocked instead of skipping the held file lock"
        )
        assert first_done.is_set()
        assert second_done.is_set()
        assert not second_write_started.is_set()
        assert first.exitcode == 0
        assert second.exitcode == 0
        assert write_count.value == 1
        assert SpillQueue(path).count() == 0


# ---------------------------------------------------------------------------
# confirmed_written (total_written counting fix — see storage/influxdb.py's
# module docstring and InfluxDBSink.total_written in
# acquisition/services/sinks.py for the full accounting rules)
# ---------------------------------------------------------------------------


class TestConfirmedWritten:
    """confirmed_written must advance ONLY from a confirmed success — the
    async batching success_callback, a successful spill replay, or a
    successful docker-exec sync write — never from write()/write_api.write()
    merely being accepted (that only means "enqueued", per C2's async
    batching design)."""

    def test_starts_at_zero(self, tmp_path):
        s = _make_storage(tmp_path)
        assert s.confirmed_written == 0

    def test_error_callback_does_not_advance_it(self, tmp_path):
        s = _make_storage(tmp_path)
        s._on_write_error(("b", "o", "ns"), _BATCH_A, RuntimeError("down"))
        s._on_write_error(("b", "o", "ns"), _BATCH_B, RuntimeError("down"))
        # 2 batches spilled (1 + 5 points) but NOTHING confirmed written.
        assert s._spill.pending_points() == 6
        assert s.confirmed_written == 0

    def test_success_callback_counts_lines_in_the_confirmed_payload(self, tmp_path):
        s = _make_storage(tmp_path)
        s._on_write_success(("b", "o", "ns"), _BATCH_A)  # 1 line
        assert s.confirmed_written == 1
        s._on_write_success(("b", "o", "ns"), _BATCH_B)  # 5 newline-joined lines
        assert s.confirmed_written == 6

    def test_success_callback_accepts_bytes_payload(self, tmp_path):
        """The real influxdb-client library passes ``data`` as bytes to the
        success callback (matching what _on_write_error already handles for
        the spill path) — must decode, not just accept str."""
        s = _make_storage(tmp_path)
        s._on_write_success(("b", "o", "ns"), _BATCH_B.encode())
        assert s.confirmed_written == 5

    def test_spill_replay_success_advances_confirmed_written_by_the_batchs_points(
        self, tmp_path,
    ):
        """The case task item 3 calls out explicitly: a batch that failed
        and spilled, then later replayed successfully, DOES count — at
        replay time, not at the original (failed) write() call."""
        s = _make_storage(tmp_path)
        s._on_write_error(("b", "o", "ns"), _BATCH_A, RuntimeError("down"))  # 1 point spilled
        s._on_write_error(("b", "o", "ns"), _BATCH_B, RuntimeError("down"))  # 5 points spilled
        assert s.confirmed_written == 0  # nothing confirmed yet, only spilled

        s.is_connected = True
        s._consecutive_failures = 0
        s.replay_api = _FakeReplayApi()
        s._drain_spill_once()

        assert s._spill.count() == 0
        # Both spilled batches (1 + 5 points) replayed successfully -> now
        # confirmed. This mirrors _spill.pending_points() before the drain.
        assert s.confirmed_written == 6

    def test_spill_replay_failure_does_not_advance_confirmed_written(self, tmp_path):
        s = _make_storage(tmp_path)
        s._on_write_error(("b", "o", "ns"), _BATCH_A, RuntimeError("down"))
        s.is_connected = True
        s._consecutive_failures = 0
        s.replay_api = _FakeReplayApi(fail_times=1)  # still down

        s._drain_spill_once()

        assert s._spill.count() == 1  # retained, not replayed
        assert s.confirmed_written == 0

    def test_confirmed_written_is_thread_safe_under_concurrent_success_callbacks(self, tmp_path):
        """_on_write_success runs on a library callback thread; concurrent
        callbacks (e.g. two in-flight batches resolving close together)
        must not lose increments to a lost-update race."""
        import threading

        s = _make_storage(tmp_path)
        line = "m,site=s1 v=1 1700000000000000000"  # 1 point per callback

        def fire():
            for _ in range(200):
                s._on_write_success(("b", "o", "ns"), line)

        threads = [threading.Thread(target=fire) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert s.confirmed_written == 8 * 200
