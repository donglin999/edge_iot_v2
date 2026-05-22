"""Tests for the M2 agent config-dispatch + task-runner path (XIU-59).

These exercise ``EdgeAgent._handle_apply_config`` and ``TaskRunner`` with
injected stubs so no real Django / WebSocket / acquisition pipeline is
spun up. The persistence side (``apply_frame``) is covered separately in
``test_state.py``.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from edge_agent.agent import EdgeAgent
from edge_agent.config import EdgeConfig
from edge_agent.protocol import (
    FRAME_CONFIG_APPLIED,
    FRAME_TASK_STATE,
    TASK_STATE_RUNNING,
    TASK_STATE_STARTING,
    TASK_STATE_STOPPED,
)
from edge_agent.runner import TaskRunner


def _cfg() -> EdgeConfig:
    return EdgeConfig(
        edge_id="edge-test", edge_token="tk",
        center_url="ws://test/ws/fleet/", labels={}, log_level="INFO",
    )


class _FakeStateStore:
    """Stand-in for EdgeStateStore that records saved frames in memory."""

    def __init__(self) -> None:
        self.saved: list[dict] = []

    def save_frame(self, frame: dict) -> None:
        self.saved.append(frame)

    def load_last_frame(self):
        return self.saved[-1] if self.saved else None


class _FakeRunner:
    """Records reconcile() calls instead of spawning task threads."""

    def __init__(self) -> None:
        self.reconciled: list[list[int]] = []

    def reconcile(self, task_ids) -> None:
        self.reconciled.append(list(task_ids))

    def running_task_ids(self):
        return []

    def stop_all(self):
        pass


def _frame(version: int = 1, task_ids=(12,)) -> dict:
    return {
        "v": "0.2",
        "type": "apply_config",
        "version": version,
        "tasks": [
            {"id": tid, "code": f"task-{tid}", "name": f"Task {tid}",
             "sample_rate_hz": 1.0, "is_active": True, "point_ids": []}
            for tid in task_ids
        ],
        "devices": [],
        "points": [],
    }


# ---------------------------------------------------------------------------
# EdgeAgent._handle_apply_config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_apply_config_persists_and_replies_ok(monkeypatch):
    store = _FakeStateStore()
    runner = _FakeRunner()
    agent = EdgeAgent(_cfg(), state_store=store, runner=runner)

    # Stub apply_frame so we don't need a real Django ORM here — the
    # state.py tests already cover the ORM side. _handle_apply_config does
    # a function-local ``from .state import apply_frame``, so patching the
    # module attribute is what the lazy import resolves against.
    from edge_agent.state import CachedConfig

    def _fake_apply(state, frame):
        state.save_frame(frame)
        return CachedConfig(
            version=int(frame["version"]),
            tasks=frame["tasks"], devices=[], points=[],
        )

    monkeypatch.setattr("edge_agent.state.apply_frame", _fake_apply)

    await agent._handle_apply_config(_frame(version=3, task_ids=(12, 13)))

    # Frame persisted, runner reconciled, config_applied enqueued.
    assert store.saved and store.saved[-1]["version"] == 3
    assert runner.reconciled == [[12, 13]]

    reply = agent._outbox.get_nowait()
    assert reply["type"] == FRAME_CONFIG_APPLIED
    assert reply["version"] == 3
    assert reply["status"] == "ok"


@pytest.mark.asyncio
async def test_handle_apply_config_replies_error_on_failure(monkeypatch):
    store = _FakeStateStore()
    runner = _FakeRunner()
    agent = EdgeAgent(_cfg(), state_store=store, runner=runner)

    def _boom(state, frame):
        raise RuntimeError("bad device protocol")

    monkeypatch.setattr("edge_agent.state.apply_frame", _boom)

    await agent._handle_apply_config(_frame(version=4))

    # Runner must NOT be reconciled on a failed apply.
    assert runner.reconciled == []
    reply = agent._outbox.get_nowait()
    assert reply["type"] == FRAME_CONFIG_APPLIED
    assert reply["status"] == "error"
    assert reply["version"] == 4
    assert "bad device protocol" in reply["error"]


def test_emit_task_state_enqueues_frame():
    """v0.3: task state transitions are emitted as ``lifecycle`` frames."""
    agent = EdgeAgent(_cfg())
    agent._emit_task_state(12, "task-12", TASK_STATE_RUNNING, None)

    frame = agent._outbox.get_nowait()
    assert frame["type"] == "lifecycle"
    assert frame["event"] == "task.running"
    assert frame["task_id"] == 12
    assert frame["task_code"] == "task-12"
    assert frame["monotonic_seq"] == 1
    assert frame["edge_id"] == "edge-test"


# ---------------------------------------------------------------------------
# TaskRunner
# ---------------------------------------------------------------------------


class _StubService:
    """A fake AcquisitionService whose run loop exits when told to."""

    def __init__(self, task, session) -> None:
        self.task = task
        self.session = session
        self._stop = threading.Event()

    def run_continuous(self):
        # Block until the runner's stop watcher flips the session — we
        # poll a short loop so the test stays fast.
        for _ in range(200):
            if self._stop.is_set():
                break
            time.sleep(0.01)
        return {"status": "completed"}


@pytest.mark.asyncio
async def test_handle_apply_config_real_orm_no_async_violation(django_edge):
    """Regression for XIU-61 defect C — apply_config must not run sync ORM
    on the asyncio loop thread.

    Uses the *real* EdgeStateStore + TaskRunner (only the AcquisitionService
    is stubbed) and drives ``_handle_apply_config`` from inside a running
    event loop. Before the fix this raised
    ``django.core.exceptions.SynchronousOnlyOperation``; after the fix the
    ORM writes are dispatched to a thread-pool executor and succeed.
    """
    from edge_agent.state import EdgeStateStore
    from configuration.models import AcqTask, Device, Point

    # This test body runs ON the asyncio loop thread, so its own ORM calls
    # must go through a worker thread too — otherwise the test setup itself
    # trips the very guard we are checking the production code against.
    def _clean():
        AcqTask.objects.all().delete()
        Device.objects.all().delete()
        Point.objects.all().delete()

    await asyncio.to_thread(_clean)

    store = EdgeStateStore(django_edge)
    services: list[_StubService] = []

    def factory(t, session):
        svc = _StubService(t, session)
        services.append(svc)
        return svc

    runner = TaskRunner(on_state=lambda *a: None, service_factory=factory)
    agent = EdgeAgent(_cfg(), state_store=store, runner=runner)

    try:
        # The production path: _handle_apply_config dispatches the sync ORM
        # work to an executor. If it ran ORM on the loop thread this await
        # would raise SynchronousOnlyOperation.
        await agent._handle_apply_config(_real_frame(version=1))

        reply = agent._outbox.get_nowait()
        assert reply["type"] == FRAME_CONFIG_APPLIED
        assert reply["status"] == "ok", reply
        assert reply["version"] == 1
        # Snapshot landed in the ORM and the task runner picked it up.
        assert await asyncio.to_thread(AcqTask.objects.filter(pk=77).exists)
        assert runner.running_task_ids() == [77]
    finally:
        for svc in services:
            svc._stop.set()
        await asyncio.to_thread(runner.stop_all)


def _real_frame(version: int = 1) -> dict:
    """A structurally-complete apply_config frame for ORM persistence."""
    return {
        "v": "0.2",
        "type": "apply_config",
        "version": version,
        "tasks": [
            {"id": 77, "code": "orm-task", "name": "ORM Task",
             "sample_rate_hz": 1.0, "is_active": True, "point_ids": [701]},
        ],
        "devices": [
            {"id": 70, "code": "orm-dev", "name": "ORM Device",
             "protocol": "modbus_tcp", "ip_address": "127.0.0.1",
             "port": 5020, "metadata": {}},
        ],
        "points": [
            {"id": 701, "device_id": 70, "code": "orm-p0", "address": "40001",
             "sample_rate_hz": 1.0, "extra": {},
             "template": {"name": "T", "english_name": "t", "unit": "",
                          "data_type": "uint16", "coefficient": 1.0,
                          "precision": 0}},
        ],
    }


def test_runner_reconcile_starts_and_stops(django_edge):
    """TaskRunner.reconcile spawns / stops task threads, emitting states."""
    from configuration.models import AcqTask

    AcqTask.objects.all().delete()
    task = AcqTask.objects.create(code="runner-task", name="Runner Task")

    events: list[tuple] = []
    lock = threading.Lock()

    def on_state(task_id, task_code, state, error):
        with lock:
            events.append((task_id, state))

    services: list[_StubService] = []

    def factory(t, session):
        svc = _StubService(t, session)
        services.append(svc)
        return svc

    runner = TaskRunner(on_state=on_state, service_factory=factory)

    # Start the task.
    runner.reconcile([task.id])
    # Give the worker thread a moment to spin up + emit running.
    deadline = time.time() + 3.0
    while time.time() < deadline:
        with lock:
            states = [s for (_tid, s) in events]
        if TASK_STATE_RUNNING in states:
            break
        time.sleep(0.02)

    with lock:
        states = [s for (_tid, s) in events]
    assert TASK_STATE_STARTING in states
    assert TASK_STATE_RUNNING in states
    assert runner.running_task_ids() == [task.id]

    # Stop the task — reconcile to an empty desired set.
    for svc in services:
        svc._stop.set()
    runner.reconcile([])

    with lock:
        states = [s for (_tid, s) in events]
    assert TASK_STATE_STOPPED in states
    assert runner.running_task_ids() == []
