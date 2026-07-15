"""Celery tasks for acquisition operations."""
from __future__ import annotations

import logging
import time
from typing import Any, Dict

from celery import shared_task
from django.utils import timezone

from acquisition import models as acq_models
from acquisition.protocols import ProtocolRegistry
from acquisition.services.acquisition_service import AcquisitionService
from configuration import models as config_models
from storage import StorageRegistry

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    acks_late=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def start_acquisition_task(
    self,
    task_id: int,
    config_version_id: int = None,
    resume_session_id: int = None,
) -> Dict[str, Any]:
    """
    Start continuous data acquisition for a task.

    H8: this is a long-running task, so it is declared ``acks_late=True`` — the
    broker message is only acked once the pipeline exits. If the worker dies
    mid-run the message is redelivered (after ``broker_transport_options
    visibility_timeout``) instead of being silently lost. To make redelivery
    safe we run an *idempotency guard*: if a RUNNING session already exists for
    the task the duplicate invocation exits immediately rather than spawning a
    second pipeline against the same devices.

    Self-healing (phase 2a): the auto-restart watchdog re-dispatches this task
    with ``resume_session_id`` set to a *dead* (stale-heartbeat) RUNNING
    session. In that case the guard **adopts** that specific session — the same
    row is reused (celery_task_id/heartbeat refreshed) instead of a new one
    being created — so exactly one pipeline runs per task and the session's
    ``restart_count`` bookkeeping persists across restarts. A genuinely live
    duplicate (fresh heartbeat, or a *different* running session) is still
    skipped.

    Args:
        task_id: ID of the AcqTask to execute
        config_version_id: Optional specific configuration version
        resume_session_id: When set, adopt this stale RUNNING session instead
            of creating a new one (used by the auto-restart watchdog).

    Returns:
        Dict with execution results
    """
    logger.info(f"Starting acquisition task {task_id}")

    try:
        # Get task configuration
        task = config_models.AcqTask.objects.prefetch_related(
            "points__device",
            "points__template"
        ).get(pk=task_id)

        if not task.is_active:
            logger.warning(f"Task {task_id} is not active, skipping")
            return {"status": "skipped", "reason": "Task is not active"}

        # Idempotency guard (H8): a redelivered message (or a racing double
        # dispatch from the API) must not start a second pipeline for a task
        # that is already being acquired.
        existing = (
            acq_models.AcquisitionSession.objects.filter(
                task_id=task_id,
                status=acq_models.AcquisitionSession.STATUS_RUNNING,
            )
            .order_by("-started_at")
            .first()
        )
        if existing is not None:
            # Auto-restart adoption: the watchdog asked us to resume THIS exact
            # stale session (its old worker died). Reuse the row so restart
            # bookkeeping persists — no second pipeline is spawned.
            if resume_session_id is not None and existing.id == resume_session_id:
                logger.warning(
                    "start_acquisition_task: adopting stale session %s for task "
                    "%s (auto-restart, invocation %s)",
                    existing.id, task_id, self.request.id,
                )
                session = existing
                session.celery_task_id = self.request.id or ""
                session.error_message = ""
                meta = session.metadata or {}
                # Refresh the heartbeat so the watchdog doesn't immediately
                # re-trigger before the resumed pipeline writes its own.
                meta["last_health_update"] = time.time()
                session.metadata = meta
                session.save(update_fields=[
                    "celery_task_id", "error_message", "metadata", "updated_at",
                ])
            else:
                # Live duplicate (fresh heartbeat, redelivery, or a different
                # session) — skip so we never run two pipelines for one task.
                logger.warning(
                    "start_acquisition_task: task %s already has a RUNNING session "
                    "%s; skipping duplicate invocation %s",
                    task_id, existing.id, self.request.id,
                )
                return {
                    "status": "skipped",
                    "reason": "already running",
                    "session_id": existing.id,
                }
        else:
            # Create acquisition session
            session = acq_models.AcquisitionSession.objects.create(
                task=task,
                status=acq_models.AcquisitionSession.STATUS_RUNNING,
                celery_task_id=self.request.id or "",
                started_at=timezone.now(),
            )

        try:
            # Use acquisition service to run the task
            service = AcquisitionService(task, session)
            result = service.run_continuous()

            # Refresh status — the user may have flipped it to STOPPED or
            # PAUSED via the API. Only force STOPPED if the loop exited for an
            # unknown reason while still marked RUNNING.
            session.refresh_from_db()
            if session.status == acq_models.AcquisitionSession.STATUS_RUNNING:
                session.status = acq_models.AcquisitionSession.STATUS_STOPPED
                session.stopped_at = timezone.now()
                session.save(update_fields=["status", "stopped_at", "updated_at"])
            elif session.stopped_at is None:
                session.stopped_at = timezone.now()
                session.save(update_fields=["stopped_at", "updated_at"])

            logger.info(f"Acquisition task {task_id} completed successfully")
            return result

        except Exception as e:
            logger.error(f"Acquisition task {task_id} failed: {e}", exc_info=True)
            session.status = acq_models.AcquisitionSession.STATUS_ERROR
            session.error_message = str(e)
            session.stopped_at = timezone.now()
            session.save(update_fields=["status", "error_message", "stopped_at", "updated_at"])
            raise

    except config_models.AcqTask.DoesNotExist:
        logger.error(f"Task {task_id} does not exist")
        return {"status": "error", "error": "Task not found"}


@shared_task(bind=True)
def stop_acquisition_task(self, session_id: int) -> Dict[str, Any]:
    """
    Stop a running acquisition session.

    Args:
        session_id: ID of the AcquisitionSession

    Returns:
        Dict with stop results
    """
    logger.info(f"Stopping acquisition session {session_id}")

    try:
        session = acq_models.AcquisitionSession.objects.get(pk=session_id)

        if session.status in [
            acq_models.AcquisitionSession.STATUS_STOPPED,
            acq_models.AcquisitionSession.STATUS_ERROR,
        ]:
            logger.warning(f"Session {session_id} is already stopped")
            return {"status": "already_stopped"}

        # Revoke the celery task if it's running. This publishes a revoke
        # command to the broker (Redis) — best-effort: if the broker is
        # unreachable we must still mark the session STOPPED, otherwise a
        # transient broker outage would make sessions impossible to stop.
        if session.celery_task_id:
            try:
                from celery import current_app
                current_app.control.revoke(session.celery_task_id, terminate=True)
            except Exception as exc:  # noqa: BLE001 — broker may be down
                logger.warning(
                    "Failed to revoke celery task %s for session %s "
                    "(broker unreachable?): %s; marking STOPPED anyway",
                    session.celery_task_id, session_id, exc,
                )

        # Update final status
        session.status = acq_models.AcquisitionSession.STATUS_STOPPED
        session.stopped_at = timezone.now()
        session.save(update_fields=["status", "stopped_at", "updated_at"])

        logger.info(f"Acquisition session {session_id} stopped successfully")
        return {"status": "stopped", "session_id": session_id}

    except acq_models.AcquisitionSession.DoesNotExist:
        logger.error(f"Session {session_id} does not exist")
        return {"status": "error", "error": "Session not found"}


@shared_task
def watchdog_recover_sessions() -> Dict[str, Any]:
    """Periodic self-healing watchdog for RUNNING acquisition sessions.

    Runs on Celery beat (~30 s). Unlike startup recovery in
    :mod:`acquisition.apps` (which only fires once, after the *process*
    restarts), this catches the case where the process stays alive but the
    acquisition Celery task/thread dies: the session is still marked RUNNING,
    yet its ``metadata.last_health_update`` heartbeat goes stale.

    For each RUNNING session:

    * fresh heartbeat  → healthy; reset its restart budget / clear any alarm.
    * brand-new (no heartbeat yet, inside the startup grace window) → leave it.
    * stale / missing heartbeat → hand to the bounded-retry restart policy.

    Safe to run when Celery beat is NOT configured — it is pure idempotent
    scanning and never raises (errors are logged, the beat loop keeps going).
    """
    from acquisition.services import restart_policy

    stale_seconds = restart_policy.HEARTBEAT_STALE_SECONDS
    now = time.time()

    try:
        running = list(
            acq_models.AcquisitionSession.objects.filter(
                status=acq_models.AcquisitionSession.STATUS_RUNNING,
            )
        )
    except Exception as exc:  # noqa: BLE001 — never let the watchdog crash beat
        logger.error("watchdog_recover_sessions scan failed: %s", exc, exc_info=True)
        return {"status": "error", "error": str(exc)}

    scanned = len(running)
    restarted = 0
    escalated = 0

    for session in running:
        try:
            age = restart_policy.heartbeat_age(session, now=now)

            if age is not None and age < stale_seconds:
                # Alive → keep the restart budget fresh, clear escalation alarm.
                restart_policy.record_recovery(session)
                continue

            if age is None and restart_policy.is_within_startup_grace(
                session, stale_seconds,
            ):
                # Brand-new session that hasn't produced a heartbeat yet.
                continue

            # Stale or long-missing heartbeat → the worker died. Restart it
            # under the bounded-retry + backoff policy.
            if restart_policy.attempt_restart(session):
                restarted += 1
            elif session.status == acq_models.AcquisitionSession.STATUS_ERROR:
                escalated += 1
        except Exception as exc:  # noqa: BLE001 — one bad session must not stop the scan
            logger.error(
                "watchdog_recover_sessions: session %s failed: %s",
                getattr(session, "id", "?"), exc, exc_info=True,
            )

    if restarted or escalated:
        logger.warning(
            "watchdog_recover_sessions: scanned=%d restarted=%d escalated=%d",
            scanned, restarted, escalated,
        )
    return {
        "status": "ok",
        "scanned": scanned,
        "restarted": restarted,
        "escalated": escalated,
    }


@shared_task
def acquire_once(task_id: int) -> Dict[str, Any]:
    """Perform a single read pass for a task (pre-flight validation).

    This is the *short* path used by ``/api/acquisition/sessions/start-task/``
    pre-flight checks and the manual "test connection" button. It is **not**
    used by the long-running pipeline — see ``start_acquisition_task`` for
    that.

    The implementation now mirrors the production pipeline: points are
    grouped by device, a :class:`ReadPlanBuilder` plan is built per device,
    and ``protocol.read_batch(group)`` is invoked once per group. This keeps
    pre-flight semantics aligned with what the continuous loop will do.

    Args:
        task_id: ID of the :class:`configuration.models.AcqTask`.

    Returns:
        On success::

            {
                "status": "success",
                "readings": [{"device_code": ..., "point_code": ...,
                              "value": ..., "quality": ...,
                              "timestamp_ns": ...}, ...],
                "count": <int>,
            }

        On failure (any device errors)::

            {"status": "error", "error": <message>, "device": <code>}
    """
    from .protocols import ProtocolRegistry
    from .services.read_plan import ReadPlanBuilder

    logger.info("Performing single acquisition for task %s", task_id)

    try:
        task = config_models.AcqTask.objects.get(pk=task_id)
    except config_models.AcqTask.DoesNotExist:
        logger.error("Task %s does not exist", task_id)
        return {"status": "error", "error": "Task not found"}

    points_qs = task.points.select_related("device", "template", "channel").all()

    # Group by device — same shape as AcquisitionService._group_points_by_device
    # but trimmed down to what read_plan needs.
    by_device: Dict[int, Dict[str, Any]] = {}
    for point in points_qs:
        bucket = by_device.setdefault(
            point.device_id, {"device": point.device, "points": []}
        )
        bucket["points"].append(point)

    if not by_device:
        return {"status": "success", "readings": [], "count": 0}

    results: list[Dict[str, Any]] = []
    for device_id, group in by_device.items():
        device = group["device"]
        cfg = {
            "source_ip": device.ip_address,
            "source_port": device.port,
            "protocol_type": device.protocol,
            **(device.metadata or {}),
            # Single-shot probe — we want to fail fast, not block the
            # caller; the continuous pipeline sets its own timeouts.
            "timeout": 5.0,
        }
        try:
            protocol = ProtocolRegistry.create(device.protocol, cfg)
            with protocol:
                read_groups = ReadPlanBuilder.build(device, group["points"])
                for rg in read_groups:
                    readings = protocol.read_batch(rg)
                    for r in readings:
                        results.append({
                            "device_code": device.code,
                            "point_code": r.point_code,
                            "value": r.value,
                            "quality": r.quality,
                            "timestamp_ns": r.timestamp_ns,
                        })
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "acquire_once failed on device %s: %s",
                device.code, exc, exc_info=True,
            )
            return {"status": "error", "error": str(exc), "device": device.code}

    return {"status": "success", "readings": results, "count": len(results)}


@shared_task
def check_protocol_connection(protocol_type: str, device_config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Test connection to a device using specified protocol.

    Args:
        protocol_type: Protocol name (e.g., 'modbus', 'plc', 'mqtt')
        device_config: Device configuration dict

    Returns:
        Dict with connection test results
    """
    logger.info(f"Testing {protocol_type} connection to {device_config.get('source_ip')}")

    try:
        protocol = ProtocolRegistry.create(protocol_type, device_config)

        with protocol:
            health = protocol.health_check()

            return {
                "status": "success" if health else "unhealthy",
                "protocol": protocol_type,
                "connected": protocol.is_connected,
                "healthy": health,
            }

    except Exception as e:
        logger.error(f"Protocol test failed: {e}", exc_info=True)
        return {
            "status": "error",
            "protocol": protocol_type,
            "error": str(e),
        }


@shared_task
def check_storage_connection(storage_type: str, storage_config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Test connection to storage backend.

    Args:
        storage_type: Storage name (e.g., 'influxdb', 'kafka')
        storage_config: Storage configuration dict

    Returns:
        Dict with storage test results
    """
    logger.info(f"Testing {storage_type} storage connection")

    try:
        storage = StorageRegistry.create(storage_type, storage_config)

        with storage:
            health = storage.health_check()

            return {
                "status": "success" if health else "unhealthy",
                "storage": storage_type,
                "connected": storage.is_connected,
                "healthy": health,
            }

    except Exception as e:
        logger.error(f"Storage test failed: {e}", exc_info=True)
        return {
            "status": "error",
            "storage": storage_type,
            "error": str(e),
        }
