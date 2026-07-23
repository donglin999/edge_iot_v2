"""Agent E - 背压/大数据量 + 入库堵塞 + 优雅停止(混沌/安全)。

范式:对端(被采对象)用真实 ``ModbusMockServer``;入库堵塞场景用一个真的、
指向不存在端口的 :class:`storage.influxdb.InfluxDBStorage`(真 TCP 拒连 +
真 influxdb-client 异步批写线程),不是打桩的 storage —— 这样才能验证
「sink 慢是否拖死采集循环」「spill 队列是否真的触发」「优雅停止是否真的不
悬挂」这几条只有走真实 I/O 才暴露得出来的行为。

覆盖(任务书 2/3/4):
  * pipeline 内部队列的实际结构 + 有界性(AlarmSink._queue / InfluxDBSink._buffer
    / WebSocketSink._buffer)。
  * InfluxDBSink._buffer 上限行为(drop-oldest)+ 真实网络失败下的 spill-to-disk
    触发。
  * 入库堵塞:指向不存在的端口(15599)不阻塞采集循环、total_written 语义。
  * 优雅停止:高负载 + InfluxDB 不可达 时 shutdown 不悬挂(有界超时收敛)。

端口段:15500-15599。绝不停共享的 edge-test-influx 容器 —— 本文件全程只用
自建的、指向真正空端口的 InfluxDBStorage 实例,spill 落在 tmp_path,不碰
backend/influx_spill.sqlite3。
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from acquisition.services.acquisition_service import AcquisitionService
from acquisition.services.pipeline import AcquisitionPipeline, ReadWorker
from acquisition.services.read_plan import Reading
from acquisition.services.sinks import (
    _ALARM_QUEUE_MAXSIZE,
    _INFLUX_BATCH_SIZE,
    _MAX_BUFFER_POINTS,
    _WS_MAX_BUFFERED_READINGS,
    AlarmSink,
    InfluxDBSink,
    Sink,
    WebSocketSink,
)
from acquisition.testing.modbus_mock_server import ModbusMockServer
from storage.influxdb import InfluxDBStorage

# pytest fixtures (create_device, create_point, create_task, create_session, ...)
from tests.fixtures.factories import *  # noqa: F401,F403


# ---------------------------------------------------------------------------
# Same thread-local DB-settings workaround as test_chaos_reconnect.py (see
# that file's comment for the full root-cause explanation) — needed by every
# test here that does real ORM writes from a spawned ReadWorker thread.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fix_thread_local_db_settings():
    from django.db import connections

    info = connections.settings.get("default")
    if info is not None:
        info.setdefault("TIME_ZONE", None)
        info.setdefault("CONN_HEALTH_CHECKS", False)
        info.setdefault("AUTOCOMMIT", True)
        info.setdefault("OPTIONS", {})
        info.setdefault("TEST", {})
        for key, val in (
            ("CHARSET", None), ("COLLATION", None),
            ("MIGRATE", True), ("MIRROR", None), ("NAME", None),
        ):
            info["TEST"].setdefault(key, val)
    yield


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _free_port() -> int:
    """A port nobody is listening on (bind-then-release => real refusal)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _reading(code: str = "p1", value=1.0, quality: str = "good") -> Reading:
    return Reading(point_code=code, value=value, timestamp_ns=time.time_ns(), quality=quality)


def _bounded_disconnect(storage, timeout: float = 5.0) -> None:
    """Test-helper twin of pipeline.py's ``_run_bounded`` fix (see the
    graceful-shutdown section below): ``InfluxDBStorage.disconnect()`` calls
    the influxdb-client's ``write_api.close()``, which blocks with NO
    timeout and was observed (empirically, while building this suite) to
    take 10s-90s+ against a genuinely unreachable backend with pending
    writes. Used in every test ``finally`` here EXCEPT the one that exists
    specifically to prove ``AcquisitionPipeline.stop()``'s own bound — that
    one exercises the real (fixed) code path directly."""
    done = threading.Event()
    t = threading.Thread(target=lambda: (storage.disconnect(), done.set()), daemon=True)
    t.start()
    done.wait(timeout=timeout)


# ---------------------------------------------------------------------------
# Agent G addition: a real, reachable InfluxDB for the positive total_written
# case (see TestTotalWrittenConfirmedCounting below). Everything else in this
# file deliberately points at dead ports; this is the one place a genuine
# backend is needed to prove the confirmed-count path actually reaches N, not
# just that it never over-counts. Own throwaway container on the G port band
# (15590) — NEVER the shared edge-test-influx (8099) — created and removed
# entirely inside the test that uses it.
# ---------------------------------------------------------------------------

_INFLUX_G_CONTAINER = "edge-test-influx-g"
_INFLUX_G_PORT = 15590
_INFLUX_G_ORG = "edge-g"
_INFLUX_G_BUCKET = "telemetry-g"
_INFLUX_G_TOKEN = "edge-test-token-g-0123456789"


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _start_real_influx_container() -> None:
    """Start a throwaway InfluxDB 2.x container for a single test.

    Image (``influxdb:2.7``) is expected to already be present locally
    (same image the shared edge-test-influx/ac_test_influxdb containers use)
    — this deliberately does not attempt a pull, which may be unavailable
    in this environment.
    """
    # Defensive: remove any same-named container leaked by a previous
    # aborted run before starting a fresh one.
    subprocess.run(
        ["docker", "rm", "-f", _INFLUX_G_CONTAINER], capture_output=True, check=False,
    )
    subprocess.run(
        [
            "docker", "run", "-d", "--name", _INFLUX_G_CONTAINER,
            "-p", f"{_INFLUX_G_PORT}:8086",
            "-e", "DOCKER_INFLUXDB_INIT_MODE=setup",
            "-e", "DOCKER_INFLUXDB_INIT_USERNAME=edge",
            "-e", "DOCKER_INFLUXDB_INIT_PASSWORD=edge12345678",
            "-e", f"DOCKER_INFLUXDB_INIT_ORG={_INFLUX_G_ORG}",
            "-e", f"DOCKER_INFLUXDB_INIT_BUCKET={_INFLUX_G_BUCKET}",
            "-e", f"DOCKER_INFLUXDB_INIT_ADMIN_TOKEN={_INFLUX_G_TOKEN}",
            "influxdb:2.7",
        ],
        check=True, capture_output=True, text=True,
    )

    deadline = time.monotonic() + 30.0
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{_INFLUX_G_PORT}/health", timeout=1.0
            ) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError) as exc:  # noqa: PERF203
            last_err = exc
        time.sleep(0.5)
    _stop_real_influx_container()
    raise RuntimeError(f"{_INFLUX_G_CONTAINER} did not become healthy in time: {last_err}")


def _stop_real_influx_container() -> None:
    subprocess.run(
        ["docker", "rm", "-f", _INFLUX_G_CONTAINER], capture_output=True, check=False,
    )


# ===========================================================================
# 1. pipeline 内部队列结构 + 有界性
# ===========================================================================


class TestInternalQueueStructures:
    """What's actually there: AlarmSink uses a bounded ``queue.Queue``;
    InfluxDBSink and WebSocketSink use a plain ``list`` under a lock. Both
    lists are (now) capped with drop-oldest — see the WebSocketSink fix
    below, which is a bug this suite found."""

    def test_alarm_sink_queue_is_a_bounded_stdlib_queue(self):
        sink = AlarmSink(SimpleNamespace(id=1), device_groups=None)
        try:
            assert sink._queue.maxsize == _ALARM_QUEUE_MAXSIZE == 1000
        finally:
            sink.close()

    def test_alarm_sink_drops_not_blocks_when_queue_full(self, monkeypatch):
        """A stalled writer must not make consume() (called from the
        ReadWorker hot path) block — queue.Full is caught and the reading is
        dropped with a throttled warning instead."""
        release = threading.Event()

        def _blocking_evaluate(self, reading, meta):
            release.wait(timeout=5.0)

        monkeypatch.setattr(AlarmSink, "_evaluate_one", _blocking_evaluate)
        device_groups = {1: {
            "device": SimpleNamespace(code="d1"),
            "points": [{"code": "p1", "coefficient": 1.0, "precision": 2}],
        }}
        sink = AlarmSink(SimpleNamespace(id=2), device_groups=device_groups)
        try:
            # First reading occupies the writer (blocked in _evaluate_one);
            # flood past maxsize with the writer stuck.
            sink.consume(_reading("p1"))
            assert _wait_until(lambda: sink._queue.unfinished_tasks >= 1, timeout=2.0)

            started = time.monotonic()
            for _ in range(_ALARM_QUEUE_MAXSIZE + 200):
                sink.consume(_reading("p1"))
            elapsed = time.monotonic() - started

            # Never blocks: flooding 1200 readings into a maxsize=1000 queue
            # while the writer is stuck must return near-instantly, not wait
            # for space.
            assert elapsed < 1.0, f"AlarmSink.consume() blocked for {elapsed:.2f}s under backpressure"
            assert sink._dropped > 0
            assert sink._queue.qsize() <= _ALARM_QUEUE_MAXSIZE
        finally:
            release.set()
            sink.close()

    def test_influx_sink_buffer_is_a_plain_bounded_list(self, monkeypatch):
        monkeypatch.setattr(InfluxDBSink, "_init_storage", lambda self: None)
        sink = InfluxDBSink(SimpleNamespace(id=3), {
            1: {"device": SimpleNamespace(code="d1", site=SimpleNamespace(code="s1"), metadata={}),
                "points": [{"code": "p1", "coefficient": 1.0, "precision": 2}]},
        })
        assert isinstance(sink._buffer, list)
        for i in range(_MAX_BUFFER_POINTS + 500):
            sink.consume(_reading("p1", value=i))
        # storage is None (never flushes) -> every point stayed in the
        # buffer, bounded at _MAX_BUFFER_POINTS, oldest dropped.
        assert len(sink._buffer) == _MAX_BUFFER_POINTS
        assert sink._dropped_total == 500
        assert sink.total_written == 0


# ===========================================================================
# 2. BUG FOUND + FIXED: WebSocketSink._buffer was unbounded
# ===========================================================================


class TestWebSocketSinkBufferBound:
    """Before the fix in this change: ``WebSocketSink._buffer`` was a plain
    list with NO cap — unlike InfluxDBSink (_MAX_BUFFER_POINTS=5000) and
    AlarmSink (queue maxsize=1000). A slow/absent channel layer or a large
    ``broadcast_interval`` at a high sample rate would grow it without limit
    -> unbounded memory growth. Fixed to drop-oldest at
    ``_WS_MAX_BUFFERED_READINGS``, mirroring InfluxDBSink's policy."""

    def test_buffer_is_capped_when_broadcast_thread_falls_behind(self):
        # A very long broadcast_interval means the background thread will
        # not drain during this test — worst case for an unbounded buffer.
        sink = WebSocketSink(SimpleNamespace(id=4), broadcast_interval=3600.0)
        try:
            n = _WS_MAX_BUFFERED_READINGS + 5000
            for i in range(n):
                sink.consume(_reading("p1", value=i))
            assert len(sink._buffer) == _WS_MAX_BUFFERED_READINGS, (
                "WebSocketSink._buffer grew past its cap — regression of the "
                "unbounded-buffer bug"
            )
            assert sink._dropped_total == n - _WS_MAX_BUFFERED_READINGS
            # Newest readings are retained (drop-oldest, not drop-newest).
            assert sink._buffer[-1].value == n - 1
        finally:
            sink.close()


# ===========================================================================
# 3. 读线程是否被 sink 阻塞 — 真实 InfluxDB 不可达端口
# ===========================================================================


def _make_unreachable_storage(tmp_path, port, **overrides):
    """Real InfluxDBStorage pointed at a genuinely closed port.

    Retry tuning is deliberately tight (bug found + fixed: these were
    hardcoded in storage/influxdb.py — see the report). With the old
    hardcoded defaults (5s/10s/20s backoff, 3 retries, PER batch) a single
    failing batch keeps the influxdb-client's internal — NOT daemonized —
    retry threads busy for tens of seconds, which was observed to prevent
    the test process itself from exiting after the test already passed.
    Fast retry settings here keep this suite's own process hygiene intact
    without weakening what's being tested (the failure path still runs for
    real, just resolves in ~100ms instead of ~35s).
    """
    cfg = {
        "url": f"http://127.0.0.1:{port}",
        "token": "chaos-token", "org": "chaos-org", "bucket": "chaos-bucket",
        # Own tmp spill file — NEVER the shared backend/influx_spill.sqlite3
        # default, which the real running dev stack may also be using.
        "spill_db_path": str(tmp_path / "chaos_spill.sqlite3"),
        "batch_size": 50, "flush_interval": 500,
        "circuit_failure_threshold": 3, "circuit_cooldown": 5.0,
        "write_retry_interval": 100, "write_max_retries": 1,
        "write_max_retry_delay": 500, "write_exponential_base": 1,
    }
    cfg.update(overrides)
    storage = InfluxDBStorage(cfg)
    storage.connect()  # fast: client construction is lazy, no network yet
    return storage


class TestReadLoopNotBlockedByUnreachableInflux:
    def test_consume_returns_fast_even_crossing_the_flush_threshold(self, tmp_path):
        """InfluxDBSink.consume() must stay non-blocking on the calling
        (worker) thread even when the batch-flush threshold is crossed
        against an unreachable backend — the async batching write_api only
        enqueues; the real HTTP attempt + retries happen off-thread."""
        port = _free_port()
        storage = _make_unreachable_storage(tmp_path, port)
        try:
            sink = InfluxDBSink.__new__(InfluxDBSink)
            sink.session = SimpleNamespace(id=5)
            sink.device_groups = {}
            sink._lock = threading.Lock()
            sink._buffer = []
            sink._last_flush = time.time()
            sink._storage = storage
            sink._total_written = 0
            sink._fail_count = 0
            sink._next_flush_at = 0.0
            sink._dropped_total = 0
            sink._flush_in_progress = False
            sink._storage_reinit_next_at = 0.0
            sink._storage_reinit_attempts = 0
            sink._storage_reinit_in_progress = False
            sink._point_meta = {"p1": {
                "device": SimpleNamespace(code="d1", site=SimpleNamespace(code="s1"), metadata={}),
                "coefficient": 1.0, "precision": 2, "template_name": "", "template_unit": "",
            }}

            worst = 0.0
            for i in range(_INFLUX_BATCH_SIZE + 20):  # crosses the flush trigger
                t0 = time.monotonic()
                sink.consume(_reading("p1", value=i))
                worst = max(worst, time.monotonic() - t0)

            assert worst < 1.0, (
                f"a single consume() call took {worst:.2f}s against an "
                f"unreachable InfluxDB — the read loop would stall this long"
            )
        finally:
            _bounded_disconnect(storage)

    @pytest.mark.django_db(transaction=True)
    def test_real_read_worker_keeps_producing_while_influx_is_unreachable(
        self, tmp_path, create_device, create_point, create_task, create_session,
    ):
        """End-to-end: a real ReadWorker against a real ModbusMockServer,
        fanning out to a real (but unreachable) InfluxDBSink alongside a
        CaptureSink. If the sink blocked the worker thread, the capture
        count would fall far short of what the sample rate implies."""
        host = "127.0.0.1"
        port = _free_port_for_modbus = None
        server = None
        for candidate_port in range(15540, 15560):
            candidate = ModbusMockServer(host=host, port=candidate_port, slave_ids=[1])
            candidate.start()
            ok = False
            for _ in range(40):
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.05)
                try:
                    s.connect((host, candidate_port))
                    ok = True
                except OSError:
                    time.sleep(0.02)
                finally:
                    s.close()
                if ok:
                    break
            if ok:
                server = candidate
                port = candidate_port
                break
            candidate.stop()
        if server is None:
            pytest.skip("could not bind a modbus mock port in 15540-15559")

        bad_influx_port = _free_port()
        storage = _make_unreachable_storage(tmp_path, bad_influx_port)

        class _CaptureSink(Sink):
            def __init__(self):
                self.n = 0
                self._lock = threading.Lock()

            def consume(self, reading):
                with self._lock:
                    self.n += 1

        try:
            device = create_device(
                code="BP-MB-01", protocol="modbus_tcp", ip=host, port=port,
                metadata={"slave_id": 1, "byte_order": "big", "timeout": 1.0},
            )
            points = [
                create_point(device=device, code=pd["code"], address=pd["address"],
                              extra={"data_type": pd["data_type"], "num": pd["num"],
                                     "function_code": 3, "unit": pd["unit"]})
                for pd in ModbusMockServer.POINTS
            ]
            task = create_task(points=points)
            session = create_session(task=task)
            group = AcquisitionService(task, session).device_groups[device.id]

            influx_sink = InfluxDBSink.__new__(InfluxDBSink)
            influx_sink.session = session
            influx_sink.device_groups = {device.id: group}
            influx_sink._lock = threading.Lock()
            influx_sink._buffer = []
            influx_sink._last_flush = time.time()
            influx_sink._storage = storage
            influx_sink._total_written = 0
            influx_sink._fail_count = 0
            influx_sink._next_flush_at = 0.0
            influx_sink._dropped_total = 0
            influx_sink._flush_in_progress = False
            influx_sink._storage_reinit_next_at = 0.0
            influx_sink._storage_reinit_attempts = 0
            influx_sink._storage_reinit_in_progress = False
            influx_sink._point_meta = {
                p["code"]: {
                    "device": group["device"], "coefficient": 1.0, "precision": 2,
                    "template_name": "", "template_unit": "",
                }
                for p in group["points"]
            }

            capture = _CaptureSink()
            sample_rate_hz = 30.0
            worker = ReadWorker(
                device=group["device"], points=group["points"],
                sinks=[capture, influx_sink], sample_rate_hz=sample_rate_hz,
                shutdown_event=threading.Event(), health_dict={}, session=session,
            )
            worker.start()
            try:
                run_seconds = 2.0
                time.sleep(run_seconds)
                # Sanity: still connected/reading, not stuck on the sink.
                assert worker.is_alive()
                expected_min = int(run_seconds * sample_rate_hz * 0.5)  # generous floor
                assert capture.n >= expected_min, (
                    f"only {capture.n} readings captured in {run_seconds}s at "
                    f"{sample_rate_hz}Hz — read loop looks stalled by the sink"
                )
            finally:
                worker.shutdown_event.set()
                worker.join(timeout=5.0)
                assert not worker.is_alive()
        finally:
            server.stop()
            _bounded_disconnect(storage)


# ===========================================================================
# 4. 入库堵塞:spill 真实触发 + total_written 语义(Agent G 已修 —— 见
#    storage/influxdb.py 的 confirmed_written)
# ===========================================================================


class TestInfluxSpillOnRealUnreachableBackend:
    def test_spill_queue_receives_batches_once_async_writer_gives_up(self, tmp_path):
        """Real network failure -> real influxdb-client async batching
        writer -> real error_callback -> real SpillQueue (SQLite, tmp_path).
        Uses the fast retry tuning from ``_make_unreachable_storage`` — with
        the library's hardcoded defaults (see that helper's docstring / the
        report) this same assertion is still true, just tens of seconds
        slower per batch; that timing difference is itself a reported
        finding, not something this test needs to wait through."""
        port = _free_port()
        storage = _make_unreachable_storage(
            tmp_path, port, batch_size=5, flush_interval=100,
        )
        try:
            line = "m,site=s1 v=1 1700000000000000000"
            for _ in range(3):
                storage.write_api.write(bucket="chaos-bucket", org="chaos-org", record=line)

            spilled = _wait_until(lambda: storage.spill_pending > 0, timeout=15.0, interval=0.2)
            assert spilled, (
                "spill queue never received a batch from a genuinely "
                "unreachable backend within 15s"
            )
            assert storage.spill_pending >= 1
        finally:
            _bounded_disconnect(storage)

    def test_total_written_stays_zero_while_spill_grows_against_dead_port(self, tmp_path):
        """FIXED (was xfail — see storage/influxdb.py's confirmed_written):
        InfluxDBSink.flush() used to treat storage.write() not raising as a
        confirmed success and immediately increment total_written + drop the
        batch from _buffer. Under the async batching write_api (C2 design,
        storage/influxdb.py) write() only means 'enqueued for a background
        HTTP attempt' — the actual success/failure is resolved later via
        _on_write_success/_on_write_error on a different thread. So
        total_written was inflated for batches that ended up failing and
        spilling to disk, breaking the documented invariant in
        acquisition_service.py ('total_points_read 来自 InfluxDBSink.
        total_written,只在写成功时递增').

        Fix: InfluxDBSink.total_written now defers to InfluxDBStorage.
        confirmed_written, which only advances on a confirmed success
        (async success_callback / docker-exec sync success / spill replay
        success) — never on write() merely being accepted. This test
        asserts (a) total_written reads 0 at every point during the run,
        not just eventually, and (b) the spill queue genuinely received the
        failed batch (proving the failure path really executed, not that
        nothing happened yet)."""
        port = _free_port()
        storage = _make_unreachable_storage(
            tmp_path, port, batch_size=50, flush_interval=200,
        )
        try:
            sink = InfluxDBSink.__new__(InfluxDBSink)
            sink.session = SimpleNamespace(id=6)
            sink.device_groups = {}
            sink._lock = threading.Lock()
            sink._buffer = []
            sink._last_flush = time.time()
            sink._storage = storage
            sink._total_written = 0
            sink._fail_count = 0
            sink._next_flush_at = 0.0
            sink._dropped_total = 0
            sink._flush_in_progress = False
            sink._storage_reinit_next_at = 0.0
            sink._storage_reinit_attempts = 0
            sink._storage_reinit_in_progress = False
            sink._point_meta = {"p1": {
                "device": SimpleNamespace(code="d1", site=SimpleNamespace(code="s1"), metadata={}),
                "coefficient": 1.0, "precision": 2, "template_name": "", "template_unit": "",
            }}

            for i in range(_INFLUX_BATCH_SIZE):  # exactly crosses the flush trigger once
                sink.consume(_reading("p1", value=i))
                # Sampled on every iteration, not just at the end: must
                # never read positive at any point, "恒 0" not "eventually 0".
                assert sink.total_written == 0

            # Wait for the async writer to actually give up and spill —
            # proves the failure path really ran to completion.
            spilled = _wait_until(lambda: storage.spill_pending > 0, timeout=15.0, interval=0.2)
            assert spilled, (
                "spill queue never received a batch from a genuinely "
                "unreachable backend within 15s"
            )
            assert storage.spill_pending >= 1

            # Even after the batch is confirmed-failed and durably spilled,
            # total_written must still read 0 — nothing was ever written.
            assert sink.total_written == 0, (
                f"total_written={sink.total_written} but nothing was durably "
                f"confirmed written (backend unreachable, batch spilled)"
            )
            assert storage.confirmed_written == 0
        finally:
            _bounded_disconnect(storage)

    def test_total_written_reaches_n_once_confirmed_by_a_real_reachable_influxdb(self, tmp_path):
        """Positive case for the same fix: against a real, reachable
        InfluxDB (own throwaway container, edge-test-influx-g on 15590 —
        never the shared edge-test-influx), N points written through the
        sink must eventually make total_written == N once the async batching
        writer actually confirms them. Proves the fix advances the counter
        for real successes, not just that it stays at 0 for failures."""
        if not _docker_available():
            pytest.skip("docker not available in this environment")

        _start_real_influx_container()
        storage = None
        try:
            cfg = {
                "url": f"http://127.0.0.1:{_INFLUX_G_PORT}",
                "token": _INFLUX_G_TOKEN,
                "org": _INFLUX_G_ORG,
                "bucket": _INFLUX_G_BUCKET,
                "spill_db_path": str(tmp_path / "g_confirm_spill.sqlite3"),
                "batch_size": 20,
                "flush_interval": 200,
            }
            storage = InfluxDBStorage(cfg)
            storage.connect()

            sink = InfluxDBSink.__new__(InfluxDBSink)
            sink.session = SimpleNamespace(id=7)
            sink.device_groups = {}
            sink._lock = threading.Lock()
            sink._buffer = []
            sink._last_flush = time.time()
            sink._storage = storage
            sink._total_written = 0
            sink._fail_count = 0
            sink._next_flush_at = 0.0
            sink._dropped_total = 0
            sink._flush_in_progress = False
            sink._storage_reinit_next_at = 0.0
            sink._storage_reinit_attempts = 0
            sink._storage_reinit_in_progress = False
            sink._point_meta = {"p1": {
                "device": SimpleNamespace(code="d1", site=SimpleNamespace(code="s1"), metadata={}),
                "coefficient": 1.0, "precision": 2, "template_name": "", "template_unit": "",
            }}

            n = 45  # below the batch trigger (50) -> exercises the flush() tail too
            for i in range(n):
                sink.consume(_reading("p1", value=float(i)))
            sink.flush()  # force the tail batch out instead of waiting for the timeout

            reached = _wait_until(lambda: sink.total_written == n, timeout=20.0, interval=0.2)
            assert reached, (
                f"total_written={sink.total_written} did not reach {n} against "
                f"a live, reachable InfluxDB within 20s"
            )
            assert storage.confirmed_written == n
            assert storage.spill_pending == 0
        finally:
            if storage is not None:
                _bounded_disconnect(storage)
            _stop_real_influx_container()


# ===========================================================================
# 5. 优雅停止:高负载 + InfluxDB 不可达 时 shutdown 不悬挂
# ===========================================================================


class TestGracefulShutdownDoesNotHang:
    @pytest.mark.django_db(transaction=True)
    def test_pipeline_stop_converges_within_bounded_time_when_influx_unreachable(
        self, tmp_path, monkeypatch, create_device, create_point, create_task, create_session,
    ):
        """BUG FOUND + FIXED (see pipeline.py's ``_run_bounded``):
        ``InfluxDBSink.close()`` -> ``storage.disconnect()`` ->
        influxdb-client's ``write_api.close()`` blocks with NO timeout,
        flushing + retrying in-flight batches. Verified in isolation this
        can block 10s+ against a genuinely unreachable backend (bounded only
        by the client's own internal retry backoff, observed to reach into
        the tens of seconds for a single batch). That previously blocked
        ``AcquisitionPipeline.stop()`` — and therefore whatever caller
        stops a session (a Celery task, an API view) — for just as long.
        Fixed by running each sink's flush()/close() on a bounded helper
        thread. This test proves ``stop()`` now converges quickly even
        though the underlying call would not have.
        """
        host = "127.0.0.1"
        server = None
        port = None
        for candidate_port in range(15560, 15580):
            candidate = ModbusMockServer(host=host, port=candidate_port, slave_ids=[1])
            candidate.start()
            ok = False
            for _ in range(40):
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.05)
                try:
                    s.connect((host, candidate_port))
                    ok = True
                except OSError:
                    time.sleep(0.02)
                finally:
                    s.close()
                if ok:
                    break
            if ok:
                server = candidate
                port = candidate_port
                break
            candidate.stop()
        if server is None:
            pytest.skip("could not bind a modbus mock port in 15560-15579")

        bad_influx_port = _free_port()
        real_bad_storage = _make_unreachable_storage(
            tmp_path, bad_influx_port, batch_size=10, flush_interval=200,
        )
        monkeypatch.setattr(InfluxDBSink, "_init_storage", lambda self: real_bad_storage)
        # WebSocketSink needs no live channel layer for this test; leave as-is
        # (consume_event degrades to a no-op when channels is unavailable).

        try:
            device = create_device(
                code="BP-STOP-01", protocol="modbus_tcp", ip=host, port=port,
                metadata={"slave_id": 1, "byte_order": "big", "timeout": 1.0},
            )
            points = [
                create_point(device=device, code=pd["code"], address=pd["address"],
                              extra={"data_type": pd["data_type"], "num": pd["num"],
                                     "function_code": 3, "unit": pd["unit"]})
                for pd in ModbusMockServer.POINTS
            ]
            task = create_task(points=points)
            session = create_session(task=task)
            device_groups = AcquisitionService(task, session).device_groups

            pipeline = AcquisitionPipeline(session, sample_rate_hz=30.0)
            pipeline.start(device_groups)
            try:
                # Let real load accumulate: buffered points, in-flight async
                # writes, maybe an alarm-sink backlog — real pending work for
                # stop() to flush/close through.
                time.sleep(1.5)
                assert pipeline.is_alive()

                started = time.monotonic()
                pipeline.stop(timeout=3.0)
                elapsed = time.monotonic() - started

                # Generous bound: worker join (<=3s) + 3 sinks x (flush+close)
                # each individually capped at 5s. Without the fix this could
                # run into minutes (bounded only by the influx client's own
                # retry backoff) — so even a loose 25s ceiling proves the fix.
                assert elapsed < 25.0, (
                    f"Pipeline.stop() took {elapsed:.1f}s against an "
                    f"unreachable InfluxDB — graceful shutdown hung"
                )
                assert not pipeline.is_alive()
            except Exception:
                pipeline.stop(timeout=1.0)
                raise
        finally:
            server.stop()
            # storage.disconnect() was already attempted (bounded) inside
            # pipeline.stop()'s InfluxDBSink.close(); do not block teardown
            # on it again here.
