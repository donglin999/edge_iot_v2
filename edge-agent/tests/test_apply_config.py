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
    agent = EdgeAgent(_cfg())
    agent._emit_task_state(12, "task-12", TASK_STATE_RUNNING, None)

    frame = agent._outbox.get_nowait()
    assert frame["type"] == FRAME_TASK_STATE
    assert frame["task_id"] == 12
    assert frame["state"] == TASK_STATE_RUNNING
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
