"""Tests for the M3 uplink path of the edge-agent (XIU-63).

Covers:
- v0.3 ``lifecycle`` / ``sample_batch`` frame builders
- ``EdgeConfig`` uplink env parsing
- ``EdgeAgent._enqueue_uplink`` monotonic-seq assignment
- ``EdgeAgent._emit_task_state`` → ``lifecycle`` frame
- ``EdgeAgent._on_sample_window`` → ``sample_batch`` frame
"""
from __future__ import annotations

import pytest

from edge_agent.agent import EdgeAgent
from edge_agent.config import EdgeConfig
from edge_agent.outbox import DurableOutbox
from edge_agent.protocol import (
    LIFECYCLE_EVENTS,
    PROTOCOL_VERSION,
    make_lifecycle,
    make_sample_batch,
    task_state_to_lifecycle_event,
)


def _cfg(**overrides) -> EdgeConfig:
    base = dict(
        edge_id="edge-test", edge_token="tk",
        center_url="ws://test/ws/fleet/", labels={}, log_level="INFO",
    )
    base.update(overrides)
    return EdgeConfig(**base)


# ---------------------------------------------------------------------------
# protocol builders
# ---------------------------------------------------------------------------


class TestProtocolBuilders:
    def test_make_lifecycle_session_event(self):
        frame = make_lifecycle(edge_id="e1", monotonic_seq=1, event="session.online")
        assert frame["type"] == "lifecycle"
        assert frame["v"] == PROTOCOL_VERSION
        assert frame["monotonic_seq"] == 1
        assert "task_id" not in frame

    def test_make_lifecycle_task_event(self):
        frame = make_lifecycle(
            edge_id="e1", monotonic_seq=4, event="task.error",
            task_id=9, task_code="t9", error="boom",
        )
        assert frame["task_id"] == 9
        assert frame["error"] == "boom"

    def test_make_lifecycle_rejects_unknown_event(self):
        with pytest.raises(ValueError):
            make_lifecycle(edge_id="e1", monotonic_seq=1, event="task.melted")

    def test_task_state_maps_to_lifecycle(self):
        assert task_state_to_lifecycle_event("running") == "task.running"
        assert task_state_to_lifecycle_event("stopped") == "task.stopped"
        for event in LIFECYCLE_EVENTS:
            assert event in ("session.online",) or event.startswith("task.")

    def test_make_sample_batch(self):
        frame = make_sample_batch(
            edge_id="e1", monotonic_seq=2, task_id=3, task_code="t3",
            samples=[{"point_code": "p", "value": 1, "quality": "good",
                      "timestamp": "2026-05-22T03:00:00Z"}],
            window_start="2026-05-22T02:59:59Z", window_end="2026-05-22T03:00:00Z",
        )
        assert frame["type"] == "sample_batch"
        assert frame["task_id"] == 3
        assert len(frame["samples"]) == 1


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


class TestUplinkConfig:
    def test_defaults_on(self):
        cfg = EdgeConfig.from_env({
            "EDGE_ID": "e", "EDGE_TOKEN": "t", "CENTER_URL": "ws://c/",
        })
        assert cfg.uplink_samples is True
        assert cfg.uplink_sample_window == 1.0

    def test_samples_can_be_disabled(self):
        cfg = EdgeConfig.from_env({
            "EDGE_ID": "e", "EDGE_TOKEN": "t", "CENTER_URL": "ws://c/",
            "EDGE_UPLINK_SAMPLES": "false",
        })
        assert cfg.uplink_samples is False

    def test_window_parsed_and_floored(self):
        cfg = EdgeConfig.from_env({
            "EDGE_ID": "e", "EDGE_TOKEN": "t", "CENTER_URL": "ws://c/",
            "EDGE_UPLINK_SAMPLE_WINDOW": "0.001",
        })
        # Clamped to the 0.05 s floor.
        assert cfg.uplink_sample_window == 0.05

    def test_window_custom_value(self):
        cfg = EdgeConfig.from_env({
            "EDGE_ID": "e", "EDGE_TOKEN": "t", "CENTER_URL": "ws://c/",
            "EDGE_UPLINK_SAMPLE_WINDOW": "2.5",
        })
        assert cfg.uplink_sample_window == 2.5


# ---------------------------------------------------------------------------
# EdgeAgent uplink seq + emitters
# ---------------------------------------------------------------------------


class TestAgentUplink:
    """M5: ``_enqueue_uplink`` now persists into the durable outbox rather
    than the (renamed) in-memory control queue."""

    @staticmethod
    def _agent(tmp_path):
        ob = DurableOutbox(str(tmp_path / "outbox.db"))
        return EdgeAgent(_cfg(), durable_outbox=ob), ob

    def test_enqueue_uplink_assigns_monotonic_seq(self, tmp_path):
        agent, ob = self._agent(tmp_path)
        for _ in range(3):
            agent._enqueue_uplink(lambda seq: {"type": "x", "monotonic_seq": seq})
        seqs = [frame["monotonic_seq"] for _, frame in ob.pending()]
        assert seqs == [1, 2, 3]

    def test_emit_task_state_produces_lifecycle_with_seq(self, tmp_path):
        agent, ob = self._agent(tmp_path)
        agent._emit_task_state(5, "t5", "running", None)
        agent._emit_task_state(5, "t5", "stopped", None)

        rows = ob.pending()
        f1, f2 = rows[0][1], rows[1][1]
        assert (f1["type"], f1["event"], f1["monotonic_seq"]) == (
            "lifecycle", "task.running", 1,
        )
        assert (f2["event"], f2["monotonic_seq"]) == ("task.stopped", 2)

    def test_emit_task_state_drops_invalid_state(self, tmp_path):
        agent, ob = self._agent(tmp_path)
        agent._emit_task_state(5, "t5", "not-a-state", None)
        assert ob.depth() == 0

    def test_on_sample_window_builds_sample_batch(self, tmp_path):
        from acquisition.services.uplink import SampleWindow

        agent, ob = self._agent(tmp_path)  # no runner → task_code falls back to id
        window = SampleWindow(
            session_id=1, task_id=42,
            window_start="2026-05-22T02:59:59Z",
            window_end="2026-05-22T03:00:00Z",
            samples=[{"point_code": "p0", "value": 1.0, "quality": "good",
                      "timestamp": "2026-05-22T03:00:00Z"}],
        )
        agent._on_sample_window(window)

        frame = ob.pending()[0][1]
        assert frame["type"] == "sample_batch"
        assert frame["task_id"] == 42
        assert frame["task_code"] == "42"
        assert frame["monotonic_seq"] == 1
        assert len(frame["samples"]) == 1

    def test_on_sample_window_ignores_missing_task_id(self, tmp_path):
        from acquisition.services.uplink import SampleWindow

        agent, ob = self._agent(tmp_path)
        window = SampleWindow(
            session_id=1, task_id=None,
            window_start="a", window_end="b", samples=[],
        )
        agent._on_sample_window(window)
        assert ob.depth() == 0
