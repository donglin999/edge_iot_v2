"""Reusable helper for raising and clearing *system* alarms.

This is the FOUNDATION for the self-healing / anomaly-reporting feature.
Unlike :mod:`acquisition.services.alarms` (which evaluates data-threshold
rules), this module creates **rule-less** alarms that represent
connectivity / session / system / lifecycle failures — problems that have
no :class:`~acquisition.models.AlarmRule` behind them.

Design contract (later phases — auto-restart watchdog, connectivity-alarm
wiring, E2E — depend on this staying stable):

* :func:`raise_system_alarm` — idempotent per ``dedup_key``. Creating an
  alarm for a problem that is *already firing* updates the existing row
  instead of piling up duplicates, so a flapping device produces ONE row.
* :func:`clear_system_alarm` — resolves every firing alarm for a
  ``dedup_key`` (firing → cleared) and returns how many it closed.
* Both are safe to call from a Celery task or a worker thread: ORM errors
  are logged, never raised, and the WebSocket broadcast can never break the
  caller (a down channel layer just means no live push).

WebSocket contract
------------------
Alarm events are pushed to the global Channels group ``acquisition_global``
(the same group the frontend's global consumer already subscribes to). The
channel-layer envelope uses ``type="alarm_event"`` so it dispatches to
``GlobalAcquisitionConsumer.alarm_event``; that consumer forwards to the
browser as::

    {"type": "alarm", "event": "created" | "cleared", "alarm": {<serialized>}}

where ``<serialized>`` is the standard :class:`AlarmSerializer` payload.
"""
from __future__ import annotations

import logging
from typing import Optional

from django.utils import timezone

from acquisition.models import Alarm

logger = logging.getLogger(__name__)

# The Channels group the frontend's global consumer subscribes to. Reused by
# later phases — keep this name stable.
ALARM_WS_GROUP = "acquisition_global"

# Browser-facing event discriminators carried in the ``event`` field.
ALARM_EVENT_CREATED = "created"
ALARM_EVENT_CLEARED = "cleared"


def _serialize_alarm(alarm: Alarm) -> dict:
    """Serialize an Alarm to the same shape the REST API returns.

    Imported lazily to avoid a circular import (``views`` imports serializers,
    tasks and protocols at module load) and to keep this module light enough
    to import from the acquisition hot path.
    """
    from acquisition.views import AlarmSerializer

    return dict(AlarmSerializer(alarm).data)


def broadcast_alarm(alarm: Alarm, event: str = ALARM_EVENT_CREATED) -> None:
    """Push an alarm create/clear event to the global WebSocket group.

    Never raises: a missing/failed channel layer (tests, headless runs)
    degrades to a no-op + log so the caller's acquisition loop is unaffected.
    """
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        channel_layer = get_channel_layer()
        if channel_layer is None:
            return
        async_to_sync(channel_layer.group_send)(
            ALARM_WS_GROUP,
            {
                "type": "alarm_event",
                "event": event,
                "alarm": _serialize_alarm(alarm),
            },
        )
    except Exception as exc:  # noqa: BLE001 — broadcasting must never break the caller
        logger.warning("alarm broadcast (%s) failed for alarm=%s: %s",
                       event, getattr(alarm, "id", "?"), exc)


def raise_system_alarm(
    *,
    category: str,
    severity: str = "warning",
    message: str,
    device_code: str = "",
    session=None,
    value: Optional[dict] = None,
    dedup_key: Optional[str] = None,
) -> Optional[Alarm]:
    """Raise (or refresh) a rule-less system/connectivity/lifecycle alarm.

    Parameters (keyword-only)
    -------------------------
    category : str
        One of :data:`Alarm.CATEGORIES` keys — ``"connectivity"``,
        ``"system"``, ``"lifecycle"`` (``"threshold"`` is reserved for
        rule-driven alarms).
    severity : str, default ``"warning"``
        One of :data:`AlarmRule.SEVERITIES` keys (``info`` / ``warning`` /
        ``critical``). Denormalized onto the alarm row.
    message : str
        Human-readable description shown in the UI.
    device_code : str, default ``""``
        Owning device code, when the problem is device-scoped.
    session : AcquisitionSession, optional
        Owning acquisition session, when the problem is session-scoped.
    value : dict, optional
        JSON-serializable context blob (diagnostics, counters, …). The model
        field is non-null, so ``None`` is stored as ``{}``.
    dedup_key : str, optional
        Logical problem key (e.g. ``"connectivity:<device_code>"``). If given
        and a FIRING alarm with this key already exists, that row is updated
        (message/value/updated_at) and returned — no duplicate is created.

    Returns
    -------
    Alarm or None
        The created/updated alarm, or ``None`` if a transient DB error was
        swallowed (logged, never raised — the acquisition loop must not break).
    """
    payload = {} if value is None else value
    try:
        if dedup_key:
            existing = Alarm.objects.filter(
                dedup_key=dedup_key, status=Alarm.STATUS_FIRING,
            ).first()
            if existing is not None:
                existing.message = message
                existing.value = payload
                existing.severity = severity
                existing.save(update_fields=["message", "value", "severity",
                                             "updated_at"])
                broadcast_alarm(existing, ALARM_EVENT_CREATED)
                return existing

        alarm = Alarm.objects.create(
            rule=None,
            session=session,
            category=category,
            severity=severity,
            dedup_key=dedup_key or "",
            point_code="",
            device_code=device_code,
            value=payload,
            status=Alarm.STATUS_FIRING,
            message=message,
        )
    except Exception as exc:  # noqa: BLE001 — reporting must not break the loop
        logger.warning("raise_system_alarm(%s, dedup_key=%s) failed: %s",
                       category, dedup_key, exc)
        return None

    logger.warning("SYSTEM ALARM %s/%s: %s", category, severity, message)
    broadcast_alarm(alarm, ALARM_EVENT_CREATED)
    return alarm


def clear_system_alarm(dedup_key: str) -> int:
    """Resolve every FIRING alarm carrying ``dedup_key`` (firing → cleared).

    Broadcasts a ``cleared`` event for each closed alarm and returns the
    number closed. An empty ``dedup_key`` is a no-op (returns 0) so it can
    never accidentally clear the whole firing set. DB errors are logged, not
    raised.
    """
    if not dedup_key:
        return 0
    try:
        qs = Alarm.objects.filter(dedup_key=dedup_key, status=Alarm.STATUS_FIRING)
        alarms = list(qs)
        if not alarms:
            return 0
        now = timezone.now()
        # ``update()`` bypasses ``auto_now``, so set updated_at explicitly.
        count = qs.update(status=Alarm.STATUS_CLEARED, cleared_at=now, updated_at=now)
    except Exception as exc:  # noqa: BLE001
        logger.warning("clear_system_alarm(%s) failed: %s", dedup_key, exc)
        return 0

    for alarm in alarms:
        # Reflect the DB transition on the in-memory copy before broadcasting.
        alarm.status = Alarm.STATUS_CLEARED
        alarm.cleared_at = now
        broadcast_alarm(alarm, ALARM_EVENT_CLEARED)
    logger.info("cleared %d system alarm(s) for dedup_key=%s", count, dedup_key)
    return count
