"""Agent E - 断线重连全链路混沌测试(真实 Modbus TCP 服务器)。

范式:被采对象是 ``acquisition.testing.modbus_mock_server.ModbusMockServer``
(真的 modbus_tk TcpServer),采集侧走真正的 ``ModbusTCPProtocol`` —— 真开
TCP、真发功能码、真读寄存器。混沌注入手段是真的 ``server.stop()`` /
``server.start()``,不是打桩协议对象。

覆盖 (任务书 1. 断线重连全链路):
  * server.stop() 后:连接告警落库(category=connectivity,
    dedup_key=connectivity:<code>)、设备状态 offline、worker 线程不死、
    持续重试不放弃。
  * server.start()(同端口)后:自动重连、告警 cleared、设备状态回到
    online、数据继续产出。
  * 反复 3 轮 stop/start,断言告警不重复堆积(dedup 生效,任意时刻至多 1
    条 FIRING)。

端口段:15500-15599(独立实例,不碰共享的 15020 demo)。
"""
from __future__ import annotations

import socket
import threading
import time

import pytest
from django.db.utils import OperationalError

from acquisition import models as acq_models
from acquisition.services.acquisition_service import AcquisitionService
from acquisition.services.device_status import compute_device_statuses
from acquisition.services.pipeline import ReadWorker
from acquisition.services.sinks import Sink
from acquisition.testing.modbus_mock_server import ModbusMockServer

# pytest fixtures (create_device, create_point, create_task, create_session, ...)
from tests.fixtures.factories import *  # noqa: F401,F403


pytestmark = pytest.mark.e2e

_PORT_RANGE = range(15500, 15600)


# ---------------------------------------------------------------------------
# Workaround for a shared test-harness bug (tests/conftest.py — not this
# agent's file, reported separately): ``django_db_modify_db_settings`` /
# pytest-django's ``transaction=True`` path replaces
# ``settings.DATABASES["default"]`` with a dict that never goes through
# Django's ``ConnectionHandler.configure_settings()`` defaulting (that only
# runs once, lazily, on ``django.db.connections.settings`` — a cached_property
# computed the first time ANY thread touches it, typically well before this
# per-test swap). A worker THREAD opening its first ORM connection therefore
# hits ``KeyError: 'TIME_ZONE'`` in
# ``BaseDatabaseWrapper.check_settings()`` — every real
# ``ReadWorker.start()`` chaos test needs a background thread doing ORM
# writes (alarms), so this is patched locally, once per test, before any
# worker thread is spawned.
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


# ---------------------------------------------------------------------------
# Port / server helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Find a bindable TCP port in the range assigned to this agent."""
    for candidate in _PORT_RANGE:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", candidate))
            return candidate
        except OSError:
            continue
        finally:
            s.close()
    raise RuntimeError("no free port in 15500-15599 for chaos-reconnect tests")


def _port_accepting(host: str, port: int, timeout: float = 0.2) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _is_transient_sqlite_lock(exc: OperationalError) -> bool:
    message = str(exc).lower()
    return message.startswith("database table is locked")


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except OperationalError as exc:
            # The worker writes alarms from another thread.  SQLite can expose
            # that short transaction as SQLITE_LOCKED instead of waiting for
            # the configured busy timeout; polling should retry that transient
            # state, while every other database error must still fail loudly.
            if not _is_transient_sqlite_lock(exc):
                raise
        time.sleep(interval)
    try:
        return predicate()
    except OperationalError as exc:
        if not _is_transient_sqlite_lock(exc):
            raise
        return False


@pytest.fixture
def chaos_server():
    """Start a real ModbusMockServer on a free port in our range.

    Reliable start/stop: retries on a fresh port if the bind check races
    with another process; the server itself is stopped in ``finally`` so no
    orphan thread/socket survives the test.
    """
    host = "127.0.0.1"
    server = None
    port = None
    last_exc = None
    for _attempt in range(5):
        try:
            port = _free_port()
            candidate = ModbusMockServer(host=host, port=port, slave_ids=[1])
            candidate.start()
            if _wait_until(lambda: _port_accepting(host, port), timeout=2.0):
                server = candidate
                break
            candidate.stop()
        except OSError as exc:  # noqa: BLE001 - port raced away between check and bind
            last_exc = exc
            continue
    if server is None:
        pytest.skip(f"could not bind a free port in 15500-15599 for chaos tests: {last_exc}")

    try:
        yield server, host, port
    finally:
        server.stop()


class _CaptureSink(Sink):
    """Records every reading fanned out to it — thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.readings = []

    def consume(self, reading) -> None:
        with self._lock:
            self.readings.append(reading)

    def count(self) -> int:
        with self._lock:
            return len(self.readings)


# ---------------------------------------------------------------------------
# Test setup helper
# ---------------------------------------------------------------------------


def _build_worker(chaos_server, create_device, create_point, create_task, create_session):
    """Wire a real Device/Point/Task/Session through AcquisitionService's real
    grouping code, then hand-build a ReadWorker with tight chaos timings so
    the disconnect -> gave_up -> alarm -> reconnect cycle completes in ~1s."""
    server, host, port = chaos_server

    device = create_device(
        code="CHAOS-MB-01",
        protocol="modbus_tcp",
        ip=host,
        port=port,
        metadata={"slave_id": 1, "byte_order": "big", "timeout": 0.5},
    )
    points = [
        create_point(
            device=device,
            code=pd["code"],
            address=pd["address"],
            extra={
                "data_type": pd["data_type"], "num": pd["num"],
                "function_code": 3, "unit": pd["unit"],
            },
        )
        for pd in ModbusMockServer.POINTS[:2]  # temperature + humidity is enough
    ]
    task = create_task(points=points)
    session = create_session(task=task)

    service = AcquisitionService(task, session)
    group = service.device_groups[device.id]

    capture = _CaptureSink()
    worker = ReadWorker(
        device=group["device"],
        points=group["points"],
        sinks=[capture],
        sample_rate_hz=20.0,          # 50ms cycle: several attempts inside connection_timeout
        shutdown_event=threading.Event(),
        health_dict={},
        max_reconnect=2,
        connection_timeout=0.5,       # detect a dead connection fast
        reconnect_backoff=0.2,        # fast retry cadence
        session=session,
    )
    return device, session, worker, capture


def _firing_alarms(dedup_key):
    return acq_models.Alarm.objects.filter(dedup_key=dedup_key, status=acq_models.Alarm.STATUS_FIRING)


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestChaosReconnect:
    def test_disconnect_reconnect_cycles_no_alarm_drift(
        self, chaos_server, create_device, create_point, create_task, create_session,
    ):
        server, host, port = chaos_server
        device, session, worker, capture = _build_worker(
            chaos_server, create_device, create_point, create_task, create_session,
        )
        dedup_key = f"connectivity:{device.code}"

        worker.start()
        try:
            # ---- initial connect: healthy, online, some data flows --------
            assert _wait_until(lambda: worker.health.get("status") == "healthy", timeout=3.0), (
                f"worker never reached healthy state, last health={worker.health}"
            )
            assert _wait_until(lambda: capture.count() > 0, timeout=3.0)
            assert compute_device_statuses([device])[device.id] == "online"
            assert _firing_alarms(dedup_key).count() == 0

            total_alarm_rows_seen = 0
            for round_no in range(1, 4):
                readings_before = capture.count()

                # ---- chaos: kill the server ---------------------------------
                server.stop()
                assert server._updater is None

                # worker must detect the outage, raise ONE persisted alarm,
                # flip the device to offline, and — critically — NOT die.
                assert _wait_until(
                    lambda: _firing_alarms(dedup_key).count() == 1, timeout=20.0,
                ), f"round {round_no}: connectivity alarm never fired (health={worker.health})"
                assert worker.is_alive(), f"round {round_no}: worker thread died while offline"
                assert compute_device_statuses([device])[device.id] == "offline"

                alarm = _firing_alarms(dedup_key).get()
                assert alarm.category == "connectivity"
                assert alarm.severity == "critical"
                assert alarm.device_code == device.code
                assert alarm.session_id == session.id

                # dedup: never more than one firing row for this key, even
                # while the worker keeps retrying every reconnect_backoff.
                time.sleep(worker.reconnect_backoff * 3)
                assert _firing_alarms(dedup_key).count() == 1, (
                    f"round {round_no}: alarm duplicated instead of deduped"
                )
                assert worker.is_alive()

                # ---- recovery: restart the server on the SAME port ----------
                server.start()
                assert _wait_until(lambda: _port_accepting(host, port), timeout=2.0), (
                    "mock server failed to rebind the same port after stop()/start()"
                )
                assert not server._stop.is_set()
                assert server._updater is not None and server._updater.is_alive()

                assert _wait_until(
                    lambda: _firing_alarms(dedup_key).count() == 0, timeout=20.0,
                ), f"round {round_no}: alarm never cleared on reconnect"
                assert compute_device_statuses([device])[device.id] == "online"
                assert _wait_until(lambda: worker.health.get("status") == "healthy", timeout=20.0)
                assert _wait_until(lambda: capture.count() > readings_before, timeout=10.0), (
                    f"round {round_no}: no fresh data after reconnect"
                )
                assert worker.is_alive()

                cleared = acq_models.Alarm.objects.filter(
                    dedup_key=dedup_key, status=acq_models.Alarm.STATUS_CLEARED,
                ).count()
                total_alarm_rows_seen = cleared + _firing_alarms(dedup_key).count()
                # one row created per down/up cycle so far, never more.
                assert total_alarm_rows_seen == round_no

            # After 3 full rounds: exactly 3 alarm rows total for this
            # dedup_key (one per cycle) — proves dedup is not stacking
            # duplicates AND not silently swallowing legitimate re-fires.
            assert acq_models.Alarm.objects.filter(dedup_key=dedup_key).count() == 3
            assert _firing_alarms(dedup_key).count() == 0
        finally:
            worker.shutdown_event.set()
            worker.join(timeout=5.0)
            assert not worker.is_alive(), "worker did not converge on shutdown"
