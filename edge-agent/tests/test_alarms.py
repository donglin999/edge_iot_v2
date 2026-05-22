"""Tests for the M4 alarm path of the edge-agent (XIU-69).

Covers:
- ``make_alarm_event`` frame builder
- ``parse_apply_config`` carrying the v0.4 ``alarm_rules`` array
- ``persist_to_orm`` mirroring + culling ``AlarmRule`` rows on the edge
- ``EdgeAgent._on_alarm_event`` → ``alarm_event`` uplink frame with a seq
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from edge_agent.agent import EdgeAgent
from edge_agent.config import EdgeConfig
from edge_agent.outbox import DurableOutbox
from edge_agent.protocol import PROTOCOL_VERSION, make_alarm_event
from edge_agent.state import parse_apply_config, persist_to_orm


def _cfg() -> EdgeConfig:
    return EdgeConfig(
        edge_id="edge-test", edge_token="tk",
        center_url="ws://test/ws/fleet/", labels={}, log_level="INFO",
    )


def _frame_with_rules(rules, version: int = 1) -> dict:
    return {
        "v": "0.4",
        "type": "apply_config",
        "version": version,
        "tasks": [],
        "devices": [],
        "points": [],
        "alarm_rules": rules,
    }


def _rule(rule_id: int, *, threshold=100.0, point="holding_0") -> dict:
    return {
        "id": rule_id,
        "name": f"rule-{rule_id}",
        "point_code": point,
        "device_code": "",
        "operator": "gt",
        "threshold": threshold,
        "threshold_high": None,
        "severity": "warning",
        "is_active": True,
        "description": "",
    }


# ---------------------------------------------------------------------------
# make_alarm_event
# ---------------------------------------------------------------------------


class TestMakeAlarmEvent:
    def test_shape(self):
        frame = make_alarm_event(
            edge_id="e1", monotonic_seq=7, rule_id=3, point_code="holding_0",
            value=137.0, device_code="plc-1", severity="critical",
            message="over",
        )
        assert frame["v"] == PROTOCOL_VERSION
        assert frame["type"] == "alarm_event"
        assert frame["monotonic_seq"] == 7
        assert frame["rule_id"] == 3
        assert frame["value"] == 137.0
        assert frame["severity"] == "critical"
        assert frame["status"] == "firing"

    def test_rejects_bad_status(self):
        with pytest.raises(ValueError):
            make_alarm_event(
                edge_id="e1", monotonic_seq=1, rule_id=1, point_code="p0",
                value=1, status="weird",
            )


# ---------------------------------------------------------------------------
# parse_apply_config — alarm_rules
# ---------------------------------------------------------------------------


class TestParseAlarmRules:
    def test_carries_alarm_rules(self):
        cached = parse_apply_config(_frame_with_rules([_rule(1), _rule(2)]))
        assert len(cached.alarm_rules) == 2
        assert {r["id"] for r in cached.alarm_rules} == {1, 2}

    def test_defaults_to_empty_when_absent(self):
        # A v0.3 center never sends the field — the edge tolerates that.
        frame = _frame_with_rules([])
        del frame["alarm_rules"]
        cached = parse_apply_config(frame)
        assert cached.alarm_rules == []

    def test_rejects_non_list_alarm_rules(self):
        bad = _frame_with_rules([])
        bad["alarm_rules"] = {"not": "a list"}
        with pytest.raises(ValueError):
            parse_apply_config(bad)


# ---------------------------------------------------------------------------
# persist_to_orm — alarm rule mirror + cull (needs Django)
# ---------------------------------------------------------------------------


def test_persist_to_orm_mirrors_alarm_rules(django_edge):
    from acquisition.models import AlarmRule

    AlarmRule.objects.all().delete()

    persist_to_orm(parse_apply_config(_frame_with_rules(
        [_rule(101, threshold=100.0), _rule(102, threshold=50.0)]
    )))

    assert AlarmRule.objects.filter(pk__in=[101, 102]).count() == 2
    assert AlarmRule.objects.get(pk=101).threshold == 100.0
    assert AlarmRule.objects.get(pk=102).operator == "gt"


def test_persist_to_orm_updates_threshold_in_place(django_edge):
    from acquisition.models import AlarmRule

    AlarmRule.objects.all().delete()

    persist_to_orm(parse_apply_config(_frame_with_rules([_rule(201, threshold=100.0)])))
    assert AlarmRule.objects.get(pk=201).threshold == 100.0

    # Center raised the threshold and re-pushed — the edge updates in place.
    persist_to_orm(parse_apply_config(_frame_with_rules(
        [_rule(201, threshold=250.0)], version=2
    )))
    assert AlarmRule.objects.count() == 1
    assert AlarmRule.objects.get(pk=201).threshold == 250.0


def test_persist_to_orm_culls_removed_alarm_rules(django_edge):
    from acquisition.models import AlarmRule

    AlarmRule.objects.all().delete()

    persist_to_orm(parse_apply_config(_frame_with_rules([_rule(301), _rule(302)])))
    assert AlarmRule.objects.count() == 2

    # A rule deleted / deactivated center-side drops out of the snapshot.
    persist_to_orm(parse_apply_config(_frame_with_rules([_rule(301)], version=2)))
    assert list(AlarmRule.objects.values_list("pk", flat=True)) == [301]


# ---------------------------------------------------------------------------
# EdgeAgent._on_alarm_event → alarm_event uplink frame
# ---------------------------------------------------------------------------


class TestAgentAlarmUplink:
    def test_on_alarm_event_enqueues_frame_with_seq(self, tmp_path):
        agent = EdgeAgent(
            _cfg(), durable_outbox=DurableOutbox(str(tmp_path / "o.db"))
        )
        event = SimpleNamespace(
            rule_id=5, point_code="holding_0", device_code="plc-1",
            value=137.0, severity="warning", status="firing",
            message="holding_0=137.0 > 100.0", fired_at="2026-05-22T03:00:00Z",
        )
        agent._on_alarm_event(event)
        agent._on_alarm_event(event)

        rows = agent._durable_outbox.pending()
        f1, f2 = rows[0][1], rows[1][1]
        assert f1["type"] == "alarm_event"
        assert f1["rule_id"] == 5
        assert f1["point_code"] == "holding_0"
        assert f1["value"] == 137.0
        # Shares the per-process monotonic uplink counter.
        assert f1["monotonic_seq"] == 1
        assert f2["monotonic_seq"] == 2
