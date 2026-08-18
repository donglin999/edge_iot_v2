"""Server-side range validation for alarm-rule writes."""

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from acquisition import models as acq_models
from acquisition.views import AlarmRuleSerializer


ALARM_RULES_URL = "/api/acquisition/alarm-rules/"


def rule_payload(**overrides):
    payload = {
        "name": "温度区间",
        "point_code": "temperature",
        "device_code": "plc-1",
        "operator": "between",
        "threshold": 10,
        "threshold_high": 20,
        "severity": "warning",
        "is_active": True,
        "description": "安全区间",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def api_client():
    return APIClient()


@pytest.mark.parametrize("operator", ["between", "outside"])
def test_serializer_requires_upper_bound_for_active_ranges(operator):
    serializer = AlarmRuleSerializer(data=rule_payload(
        operator=operator, threshold_high=None,
    ))

    assert not serializer.is_valid()
    assert "threshold_high" in serializer.errors


@pytest.mark.parametrize("operator", ["between", "outside"])
def test_serializer_requires_lower_bound_for_active_ranges(operator):
    serializer = AlarmRuleSerializer(data=rule_payload(
        operator=operator, threshold=None,
    ))

    assert not serializer.is_valid()
    assert "threshold" in serializer.errors


@pytest.mark.parametrize("operator", ["between", "outside"])
def test_serializer_rejects_inverted_active_ranges(operator):
    serializer = AlarmRuleSerializer(data=rule_payload(
        operator=operator, threshold=30, threshold_high=20,
    ))

    assert not serializer.is_valid()
    assert serializer.errors["threshold_high"] == [
        "阈值上限必须大于或等于阈值下限。",
    ]


@pytest.mark.parametrize("operator", ["between", "outside"])
def test_serializer_accepts_ordered_active_ranges(operator):
    serializer = AlarmRuleSerializer(data=rule_payload(operator=operator))

    assert serializer.is_valid(), serializer.errors


def test_serializer_keeps_single_threshold_and_inactive_draft_compatible():
    single_threshold = AlarmRuleSerializer(data=rule_payload(
        operator="gt", threshold_high=None,
    ))
    inactive_draft = AlarmRuleSerializer(data=rule_payload(
        operator="outside", threshold_high=None, is_active=False,
    ))

    assert single_threshold.is_valid(), single_threshold.errors
    assert inactive_draft.is_valid(), inactive_draft.errors


def test_serializer_accepts_equal_range_bounds():
    serializer = AlarmRuleSerializer(data=rule_payload(
        threshold=20, threshold_high=20,
    ))

    assert serializer.is_valid(), serializer.errors


@pytest.mark.django_db
def test_serializer_partial_update_uses_unchanged_instance_bounds():
    rule = acq_models.AlarmRule.objects.create(**rule_payload())
    serializer = AlarmRuleSerializer(
        rule,
        data={"threshold": 25},
        partial=True,
    )

    assert not serializer.is_valid()
    assert "threshold_high" in serializer.errors


@pytest.mark.django_db
@pytest.mark.parametrize("operator", ["between", "outside"])
def test_api_create_rejects_active_range_without_upper_bound(api_client, operator):
    response = api_client.post(
        ALARM_RULES_URL,
        rule_payload(operator=operator, threshold_high=None),
        format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "threshold_high" in response.data
    assert not acq_models.AlarmRule.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("operator", ["between", "outside"])
def test_api_create_accepts_ordered_active_range(api_client, operator):
    response = api_client.post(
        ALARM_RULES_URL,
        rule_payload(operator=operator),
        format="json",
    )

    assert response.status_code == status.HTTP_201_CREATED
    rule = acq_models.AlarmRule.objects.get(pk=response.data["id"])
    assert rule.operator == operator
    assert rule.threshold == 10
    assert rule.threshold_high == 20


@pytest.mark.django_db
def test_api_full_update_rejects_inverted_range_without_mutating_rule(api_client):
    rule = acq_models.AlarmRule.objects.create(**rule_payload())

    response = api_client.put(
        f"{ALARM_RULES_URL}{rule.pk}/",
        rule_payload(name="倒置区间", threshold=30, threshold_high=20),
        format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "threshold_high" in response.data
    rule.refresh_from_db()
    assert rule.name == "温度区间"
    assert rule.threshold == 10
    assert rule.threshold_high == 20


@pytest.mark.django_db
def test_api_partial_update_rejects_effective_inverted_range(api_client):
    rule = acq_models.AlarmRule.objects.create(**rule_payload())

    response = api_client.patch(
        f"{ALARM_RULES_URL}{rule.pk}/",
        {"threshold": 25},
        format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "threshold_high" in response.data
    rule.refresh_from_db()
    assert rule.threshold == 10


@pytest.mark.django_db
def test_api_partial_update_accepts_equal_range_bounds(api_client):
    rule = acq_models.AlarmRule.objects.create(**rule_payload())

    response = api_client.patch(
        f"{ALARM_RULES_URL}{rule.pk}/",
        {"threshold": 20},
        format="json",
    )

    assert response.status_code == status.HTTP_200_OK
    assert response.data["threshold"] == 20
    assert response.data["threshold_high"] == 20


@pytest.mark.django_db
def test_api_cannot_activate_an_invalid_range_draft(api_client):
    rule = acq_models.AlarmRule.objects.create(**rule_payload(
        operator="outside", threshold_high=None, is_active=False,
    ))

    response = api_client.patch(
        f"{ALARM_RULES_URL}{rule.pk}/",
        {"is_active": True},
        format="json",
    )

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert "threshold_high" in response.data
    rule.refresh_from_db()
    assert rule.is_active is False


@pytest.mark.django_db
def test_api_keeps_single_threshold_rule_compatible(api_client):
    create_response = api_client.post(
        ALARM_RULES_URL,
        rule_payload(operator="gt", threshold_high=None),
        format="json",
    )
    assert create_response.status_code == status.HTTP_201_CREATED

    update_response = api_client.patch(
        f"{ALARM_RULES_URL}{create_response.data['id']}/",
        {"threshold": 15},
        format="json",
    )

    assert update_response.status_code == status.HTTP_200_OK
    assert update_response.data["threshold"] == 15
    assert update_response.data["threshold_high"] is None
