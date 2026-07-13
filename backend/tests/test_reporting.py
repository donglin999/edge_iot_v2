"""Tests for the reusable system-alarm reporting helper (phase 1 foundation).

Covers :mod:`acquisition.services.reporting` plus the model/serializer changes
that let an Alarm represent a rule-less connectivity/system/lifecycle failure.
"""
import pytest

from acquisition import models as acq_models
from acquisition.services import reporting
from acquisition.services.alarms import evaluate_readings
from acquisition.views import AlarmSerializer

# pytest fixtures (create_session, create_task, …)
from tests.fixtures.factories import *  # noqa: F401,F403


@pytest.mark.django_db
class TestRaiseSystemAlarm:
    def test_creates_ruleless_firing_alarm(self):
        alarm = reporting.raise_system_alarm(
            category="connectivity",
            severity="critical",
            message="设备 PLC-01 连接丢失",
            device_code="PLC-01",
            dedup_key="connectivity:PLC-01",
        )

        assert alarm is not None
        assert alarm.pk is not None
        assert alarm.rule is None
        assert alarm.category == "connectivity"
        assert alarm.severity == "critical"
        assert alarm.status == acq_models.Alarm.STATUS_FIRING
        assert alarm.device_code == "PLC-01"
        assert alarm.dedup_key == "connectivity:PLC-01"
        # value is a non-null JSONField — None must be stored as {}.
        assert alarm.value == {}

    def test_default_severity_is_warning(self):
        alarm = reporting.raise_system_alarm(
            category="system",
            message="磁盘将满",
        )
        assert alarm.severity == "warning"
        assert alarm.category == "system"

    def test_value_dict_persisted(self):
        alarm = reporting.raise_system_alarm(
            category="system",
            message="队列积压",
            value={"queue_depth": 4200},
        )
        alarm.refresh_from_db()
        assert alarm.value == {"queue_depth": 4200}

    def test_dedup_updates_existing_instead_of_duplicating(self):
        first = reporting.raise_system_alarm(
            category="connectivity",
            severity="warning",
            message="连接不稳定",
            device_code="PLC-02",
            dedup_key="connectivity:PLC-02",
            value={"fails": 1},
        )
        second = reporting.raise_system_alarm(
            category="connectivity",
            severity="critical",
            message="连接彻底丢失",
            device_code="PLC-02",
            dedup_key="connectivity:PLC-02",
            value={"fails": 9},
        )

        # Same row, not a duplicate.
        assert second.pk == first.pk
        firing = acq_models.Alarm.objects.filter(
            dedup_key="connectivity:PLC-02",
            status=acq_models.Alarm.STATUS_FIRING,
        )
        assert firing.count() == 1

        # The row was updated in place.
        row = firing.get()
        assert row.message == "连接彻底丢失"
        assert row.severity == "critical"
        assert row.value == {"fails": 9}

    def test_no_dedup_key_always_creates(self):
        a = reporting.raise_system_alarm(category="system", message="a")
        b = reporting.raise_system_alarm(category="system", message="b")
        assert a.pk != b.pk

    def test_broadcast_failure_never_raises(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise RuntimeError("channel layer exploded")

        monkeypatch.setattr(reporting, "_serialize_alarm", _boom)
        # Must still create + return the alarm despite broadcast blowing up.
        alarm = reporting.raise_system_alarm(category="system", message="x")
        assert alarm is not None
        assert alarm.pk is not None


@pytest.mark.django_db
class TestClearSystemAlarm:
    def test_firing_to_cleared(self):
        reporting.raise_system_alarm(
            category="connectivity",
            message="断线",
            dedup_key="connectivity:PLC-03",
        )
        count = reporting.clear_system_alarm("connectivity:PLC-03")
        assert count == 1

        alarm = acq_models.Alarm.objects.get(dedup_key="connectivity:PLC-03")
        assert alarm.status == acq_models.Alarm.STATUS_CLEARED
        assert alarm.cleared_at is not None

    def test_clear_no_firing_returns_zero(self):
        assert reporting.clear_system_alarm("nonexistent:key") == 0

    def test_empty_dedup_key_is_noop(self):
        # A blank key must never clear the whole firing set.
        reporting.raise_system_alarm(category="system", message="keep me")
        assert reporting.clear_system_alarm("") == 0
        assert acq_models.Alarm.objects.filter(
            status=acq_models.Alarm.STATUS_FIRING,
        ).count() == 1


@pytest.mark.django_db
class TestThresholdAlarmCompat:
    def test_threshold_alarm_carries_category_and_severity(self, create_session):
        session = create_session()
        rule = acq_models.AlarmRule.objects.create(
            name="温度过高",
            point_code="temp",
            operator="gt",
            threshold=80.0,
            severity="critical",
        )

        fired = evaluate_readings(session, "DEV-1", [{"code": "temp", "value": 95.0}])

        assert len(fired) == 1
        alarm = fired[0]
        assert alarm.rule_id == rule.id
        assert alarm.category == "threshold"
        assert alarm.severity == "critical"  # copied from the rule
        assert alarm.status == acq_models.Alarm.STATUS_FIRING


@pytest.mark.django_db
class TestSerializer:
    def test_serializes_ruleless_alarm_without_error(self):
        alarm = reporting.raise_system_alarm(
            category="connectivity",
            severity="warning",
            message="设备离线",
            device_code="PLC-9",
            dedup_key="connectivity:PLC-9",
        )
        data = AlarmSerializer(alarm).data

        assert data["rule"] is None
        assert data["rule_name"] is None
        assert data["category"] == "connectivity"
        assert data["severity"] == "warning"
        assert data["message"] == "设备离线"
        assert data["device_code"] == "PLC-9"
        assert data["dedup_key"] == "connectivity:PLC-9"
        assert data["status"] == acq_models.Alarm.STATUS_FIRING
