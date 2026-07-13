"""Auto-restart (self-healing) policy for acquisition sessions.

Phase 2a of the self-healing feature. This module encapsulates the *policy*
of restarting a dead acquisition session with a **bounded retry + exponential
backoff**, escalating to a critical system alarm once the retry budget is
exhausted (so a genuinely broken device produces one alarm instead of an
endless crash-loop).

It is shared by two callers:

* startup recovery — :meth:`acquisition.apps.AcquisitionConfig._recover_sessions`
  runs once after Django boots and reconciles sessions that were RUNNING when
  the process died.
* the periodic watchdog — :func:`acquisition.tasks.watchdog_recover_sessions`
  runs every ~30 s and catches the case where the *process* stayed up but the
  acquisition Celery task/thread died (startup recovery can never see this).

Bookkeeping lives in ``session.metadata`` (a JSONField, so no migration):

* ``restart_count``      — auto-restarts performed for this session's lifecycle.
* ``last_restart_at``    — wall-clock epoch of the last re-dispatch (backoff gate).
* ``restart_history``    — bounded list of ``{"at": epoch, "count": n}`` records.

Timestamps are **wall-clock** (:func:`time.time`) on purpose: they are
persisted and compared across processes/restarts, where ``time.monotonic`` is
meaningless.

No-duplicate-pipeline guarantee
-------------------------------
A re-dispatch keeps the session RUNNING and passes ``resume_session_id`` to
:func:`~acquisition.tasks.start_acquisition_task`. That task's single-RUNNING-
per-task guard treats a *fresh-heartbeat* RUNNING session as a live duplicate
(skips) but *adopts* the specific stale session named by ``resume_session_id``
(reuses the same row instead of spawning a second pipeline). Because the same
session row is reused across restarts, ``restart_count`` accumulates naturally
and the crash-loop cap actually triggers.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from django.conf import settings
from django.utils import timezone

from acquisition.models import AcquisitionSession
from acquisition.services.reporting import clear_system_alarm, raise_system_alarm

logger = logging.getLogger(__name__)

# --- Policy constants (all overridable via Django settings) -----------------

#: Maximum number of automatic restarts before a session is declared fatal.
MAX_AUTO_RESTARTS: int = int(getattr(settings, "ACQUISITION_MAX_AUTO_RESTARTS", 5))

#: Exponential-backoff base (seconds): wait ~= base * 2**restart_count.
RESTART_BACKOFF_BASE_SECONDS: float = float(
    getattr(settings, "ACQUISITION_RESTART_BACKOFF_BASE_SECONDS", 10.0)
)

#: Backoff ceiling (seconds) — never wait longer than this between attempts.
RESTART_BACKOFF_CAP_SECONDS: float = float(
    getattr(settings, "ACQUISITION_RESTART_BACKOFF_CAP_SECONDS", 300.0)
)

#: A RUNNING session whose ``last_health_update`` is older than this (seconds)
#: is treated as dead. Shared with :mod:`acquisition.apps` / the watchdog.
HEARTBEAT_STALE_SECONDS: float = float(
    getattr(settings, "ACQUISITION_HEARTBEAT_STALE_SECONDS", 60.0)
)


def escalation_dedup_key(session) -> str:
    """Stable dedup key for the "restart budget exhausted" alarm."""
    return f"session-restart-failed:{session.id}"


def backoff_seconds(restart_count: int) -> float:
    """Backoff to observe *before* the next attempt, given attempts so far.

    ``restart_count`` is the number of restarts already performed. The first
    restart is gated only by ``last_restart_at`` being unset (immediate); every
    subsequent one waits ``min(base * 2**count, cap)``.
    """
    count = max(0, int(restart_count))
    return min(RESTART_BACKOFF_BASE_SECONDS * (2 ** count), RESTART_BACKOFF_CAP_SECONDS)


def heartbeat_age(session, now: Optional[float] = None) -> Optional[float]:
    """Age in seconds of the session's last heartbeat, or ``None`` if never."""
    now = time.time() if now is None else now
    last = (session.metadata or {}).get("last_health_update")
    if last is None:
        return None
    try:
        return now - float(last)
    except (TypeError, ValueError):
        return None


def is_within_startup_grace(session, grace_seconds: float, now_dt=None) -> bool:
    """True for a brand-new session that has not had time to heartbeat yet.

    Only meaningful when the session has *no* heartbeat at all — a session that
    has already reported a heartbeat is judged on its heartbeat age instead.
    """
    if not session.started_at:
        # No heartbeat and no start time — cannot judge; treat as in-grace so
        # we neither restart nor escalate a session we know nothing about.
        return True
    now_dt = timezone.now() if now_dt is None else now_dt
    return (now_dt - session.started_at).total_seconds() < grace_seconds


def record_recovery(session) -> None:
    """A session is healthy again → reset its restart budget and clear alarm.

    Idempotent and cheap: only writes the row when ``restart_count`` was
    non-zero, and always clears any firing escalation alarm (a no-op when none
    is firing). This gives a device that recovers then fails again months later
    a fresh restart budget rather than an already-exhausted one.
    """
    meta = dict(session.metadata or {})
    if meta.get("restart_count"):
        meta["restart_count"] = 0
        meta.pop("last_restart_at", None)
        session.metadata = meta
        try:
            session.save(update_fields=["metadata", "updated_at"])
        except Exception as exc:  # noqa: BLE001 — recovery must not break the caller
            logger.warning("record_recovery save failed for session %s: %s",
                           session.id, exc)
        logger.info("Session %s recovered — restart budget reset", session.id)
    clear_system_alarm(escalation_dedup_key(session))


def attempt_restart(session) -> bool:
    """Try to auto-restart a dead session under the bounded-retry policy.

    Returns ``True`` when a re-dispatch was issued, ``False`` otherwise (budget
    exhausted → escalated, or still inside the backoff window).
    """
    meta = dict(session.metadata or {})
    restart_count = int(meta.get("restart_count", 0) or 0)
    last_restart_at = meta.get("last_restart_at")

    # 1) Budget exhausted → mark fatal + escalate (idempotent alarm).
    if restart_count >= MAX_AUTO_RESTARTS:
        return _escalate(session, meta, restart_count)

    # 2) Backoff gate — the first restart (last_restart_at unset) is immediate.
    now = time.time()
    if last_restart_at is not None:
        try:
            elapsed = now - float(last_restart_at)
        except (TypeError, ValueError):
            elapsed = None
        if elapsed is not None:
            wait = backoff_seconds(restart_count)
            if elapsed < wait:
                logger.info(
                    "Session %s restart deferred: %.1fs since last attempt < "
                    "%.1fs backoff (count=%d)",
                    session.id, elapsed, wait, restart_count,
                )
                return False

    # 3) Persist the bumped bookkeeping BEFORE dispatch so a crash mid-dispatch
    #    still counts the attempt (fail safe toward the cap, never away from it).
    new_count = restart_count + 1
    meta["restart_count"] = new_count
    meta["last_restart_at"] = now
    history = list(meta.get("restart_history") or [])
    history.append({"at": now, "count": new_count})
    meta["restart_history"] = history[-20:]
    session.metadata = meta
    session.save(update_fields=["metadata", "updated_at"])

    # 4) Re-dispatch. resume_session_id makes start_acquisition_task adopt THIS
    #    session instead of spawning a second pipeline (see module docstring).
    task_id = session.task_id
    config_version_id = meta.get("config_version_id")
    try:
        from acquisition.tasks import start_acquisition_task

        start_acquisition_task.delay(
            task_id, config_version_id, resume_session_id=session.id,
        )
    except Exception as exc:  # noqa: BLE001 — a broker hiccup must not raise here
        logger.error("Session %s restart dispatch failed: %s",
                     session.id, exc, exc_info=True)
        return False

    logger.warning(
        "Session %s auto-restart %d/%d dispatched (task=%s)",
        session.id, new_count, MAX_AUTO_RESTARTS, task_id,
    )
    return True


def _escalate(session, meta: dict, restart_count: int) -> bool:
    """Mark the session fatally ERROR and raise the critical escalation alarm."""
    session.status = AcquisitionSession.STATUS_ERROR
    session.error_message = (
        f"自动重启 {MAX_AUTO_RESTARTS} 次仍失败，已停止（防止崩溃循环）"
    )
    session.stopped_at = session.stopped_at or timezone.now()
    meta["restart_capped_at"] = time.time()
    session.metadata = meta
    session.save(update_fields=[
        "status", "error_message", "stopped_at", "metadata", "updated_at",
    ])

    raise_system_alarm(
        category="system",
        severity="critical",
        session=session,
        device_code="",
        message=f"会话 {session.id} 自动重启 {MAX_AUTO_RESTARTS} 次仍失败，已停止",
        dedup_key=escalation_dedup_key(session),
        value={"restart_count": restart_count},
    )
    logger.error(
        "Session %s exceeded MAX_AUTO_RESTARTS (%d) — escalated to critical alarm",
        session.id, MAX_AUTO_RESTARTS,
    )
    return False
