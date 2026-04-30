"""Alarm evaluation invoked from the acquisition loop.

Kept stateless and dependency-light so it can run inside the worker thread
without adding latency. The "did this rule already fire?" dedup is handled
by reusing an open ``Alarm`` row keyed by (rule, session, point_code).
"""
from __future__ import annotations

import logging
from typing import Iterable, List, Optional

from django.utils import timezone

from acquisition.models import Alarm, AlarmRule, AcquisitionSession

logger = logging.getLogger(__name__)


def _coerce_number(value) -> Optional[float]:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _evaluate(rule: AlarmRule, value: float) -> bool:
    op = rule.operator
    th = rule.threshold
    th2 = rule.threshold_high
    if op == "gt":
        return th is not None and value > th
    if op == "ge":
        return th is not None and value >= th
    if op == "lt":
        return th is not None and value < th
    if op == "le":
        return th is not None and value <= th
    if op == "eq":
        return th is not None and value == th
    if op == "ne":
        return th is not None and value != th
    if op == "between":
        return th is not None and th2 is not None and th <= value <= th2
    if op == "outside":
        return th is not None and th2 is not None and (value < th or value > th2)
    return False


def evaluate_readings(
    session: AcquisitionSession,
    device_code: str,
    readings: Iterable[dict],
) -> List[Alarm]:
    """Check `readings` against active rules; create/clear alarms as needed.

    ``readings`` is the same shape ``protocol.read_points`` returns:
    ``{"code": ..., "value": ..., "quality": ..., ...}``.
    """
    rules = list(AlarmRule.objects.filter(is_active=True))
    if not rules:
        return []

    fired: List[Alarm] = []
    by_code: dict[str, List[AlarmRule]] = {}
    for r in rules:
        # An empty device_code on the rule means "any device with this point"
        if r.device_code and r.device_code != device_code:
            continue
        by_code.setdefault(r.point_code, []).append(r)

    for reading in readings:
        code = reading.get("code")
        if not code or code not in by_code:
            continue
        value = _coerce_number(reading.get("value"))
        if value is None:
            continue
        for rule in by_code[code]:
            triggered = _evaluate(rule, value)
            existing = Alarm.objects.filter(
                rule=rule,
                point_code=code,
                device_code=device_code,
                status=Alarm.STATUS_FIRING,
            ).first()
            if triggered and not existing:
                alarm = Alarm.objects.create(
                    rule=rule,
                    session=session,
                    point_code=code,
                    device_code=device_code,
                    value=value,
                    message=(
                        f"{code}={value} 触发规则 [{rule.name}] "
                        f"{rule.get_operator_display()} {rule.threshold}"
                    ),
                )
                fired.append(alarm)
                logger.warning("ALARM %s: %s", rule.severity, alarm.message)
            elif not triggered and existing:
                existing.status = Alarm.STATUS_CLEARED
                existing.cleared_at = timezone.now()
                existing.save(update_fields=["status", "cleared_at", "updated_at"])
                logger.info("ALARM cleared: rule=%s code=%s value=%s", rule.name, code, value)
    return fired
