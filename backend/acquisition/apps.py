"""App configuration for acquisition module."""
import logging
from django.apps import AppConfig
from django.db import transaction

logger = logging.getLogger(__name__)


class AcquisitionConfig(AppConfig):
    """Configuration for the acquisition application."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "acquisition"
    verbose_name = "数据采集"

    # A RUNNING session whose ``updated_at`` hasn't moved in this many
    # minutes is treated as an orphan on the next startup. The acquisition
    # loop refreshes ``updated_at`` every ~5s, so 10 min leaves plenty of
    # slack for transient pauses (long Modbus timeouts, GC pauses, ...).
    ORPHAN_STALE_MINUTES = 10
    HEARTBEAT_STALE_SECONDS = 60.0

    def ready(self):
        """Import signals and recover running acquisition sessions."""
        import acquisition.signals  # noqa

        # Only run recovery / orphan cleanup in the *final* process —
        # skip Django runserver's autoreload parent, but allow runserver
        # children (RUN_MAIN=true) and celery workers (detected via argv).
        import os
        import sys
        is_runserver_child = os.environ.get('RUN_MAIN') == 'true'
        is_celery_worker = any('celery' in str(arg).lower() for arg in sys.argv)
        if not is_runserver_child and not is_celery_worker:
            return

        # Orphan session cleanup runs first — purely DB work, no external I/O.
        # Safe to call from both runserver and celery; idempotent (UPDATE ...
        # WHERE status='running' AND updated_at < cutoff).
        self._cleanup_orphan_sessions()

        # Register shutdown handler
        self._register_shutdown_handler()

        # Recover running sessions after Django startup
        self._recover_sessions()

    def _cleanup_orphan_sessions(self):
        """Mark stale RUNNING sessions as ERROR on startup.

        A session is considered "orphan" when its ``updated_at`` has not
        moved in ``ORPHAN_STALE_MINUTES`` minutes. The acquisition loop
        bumps ``updated_at`` every ~5s via ``_update_sqlite_metadata``, so
        anything quieter than that is almost certainly a worker that
        crashed / OOM'd / was kill -9'd before it could update its own
        status.

        Distinct from :meth:`_recover_sessions`, which examines the
        in-metadata heartbeat. Both can run; whichever notices first wins
        and the other becomes a no-op.
        """
        from datetime import timedelta

        from django.db.utils import OperationalError
        from django.utils import timezone

        from acquisition.models import AcquisitionSession

        try:
            cutoff = timezone.now() - timedelta(minutes=self.ORPHAN_STALE_MINUTES)
            count = AcquisitionSession.objects.filter(
                status=AcquisitionSession.STATUS_RUNNING,
                updated_at__lt=cutoff,
            ).update(
                status=AcquisitionSession.STATUS_ERROR,
                error_message="Orphan session cleaned up at startup (worker likely crashed)",
                stopped_at=timezone.now(),
            )
            if count:
                logger.warning(
                    "Cleaned up %d orphan running sessions at startup", count,
                )
        except OperationalError:
            # Tables not migrated yet — first-time setup, normal.
            pass
        except Exception as exc:  # noqa: BLE001
            # Never block startup over cleanup.
            logger.error("Orphan cleanup failed: %s", exc, exc_info=True)

    def _recover_sessions(self):
        """Reconcile RUNNING sessions on startup using the loop's heartbeat.

        The acquisition loop writes ``metadata.last_health_update`` every
        ``SQLITE_METADATA_INTERVAL`` seconds (10 s by default). On Django
        startup we check each RUNNING session:

        * Fresh heartbeat (< 60 s) → worker is still alive in another
          container; do nothing, the existing celery task remains in charge.
        * Stale heartbeat or missing → the worker died (process kill, OOM,
          power loss). Mark the session as ERROR with the reason so the
          history reflects what actually happened. We do NOT auto-restart;
          the operator can press "启动" again from the UI when ready.

        The previous implementation deleted RUNNING sessions and re-spawned
        them, which masked silent crashes and lost history.
        """
        try:
            import time

            from acquisition.models import AcquisitionSession

            running = list(AcquisitionSession.objects.filter(
                status=AcquisitionSession.STATUS_RUNNING,
            ))
            if not running:
                logger.info("No running sessions to recover")
                return

            now = time.time()
            for session in running:
                last_update = (session.metadata or {}).get("last_health_update")
                age = now - float(last_update) if last_update else None

                if age is not None and age < self.HEARTBEAT_STALE_SECONDS:
                    logger.info(
                        "Session %s heartbeat fresh (%.1fs ago) — leaving as RUNNING",
                        session.id, age,
                    )
                    continue

                # No heartbeat yet → could be a brand-new session that hasn't
                # finished its first cycle. Allow a grace period based on
                # session age before declaring it dead.
                if age is None and session.started_at:
                    session_age = (timezone_now() - session.started_at).total_seconds()
                    if session_age < self.HEARTBEAT_STALE_SECONDS:
                        logger.info(
                            "Session %s no heartbeat yet but only %.1fs old — leaving",
                            session.id, session_age,
                        )
                        continue

                reason = (
                    f"Worker heartbeat stale ({age:.0f}s ago)" if age is not None
                    else "Worker never reported a heartbeat"
                )
                logger.warning("Session %s: %s — marking ERROR", session.id, reason)
                session.status = AcquisitionSession.STATUS_ERROR
                session.error_message = reason
                session.stopped_at = session.stopped_at or timezone_now()
                meta = session.metadata or {}
                meta["recovered_at_startup"] = timezone_now().isoformat()
                session.metadata = meta
                session.save(update_fields=[
                    "status", "error_message", "stopped_at", "metadata", "updated_at",
                ])
        except Exception as exc:  # noqa: BLE001
            logger.error("Session recovery failed: %s", exc, exc_info=True)

    def _register_shutdown_handler(self):
        """
        Register signal handlers for graceful shutdown.

        This ensures that when Django stops (via SIGTERM or SIGINT),
        all running Celery acquisition tasks are also stopped gracefully.
        """
        import signal
        import sys

        def shutdown_handler(signum, frame):
            """Handle shutdown signals by stopping all running sessions."""
            signal_name = 'SIGTERM' if signum == signal.SIGTERM else 'SIGINT'
            logger.info(f"Received {signal_name}, stopping all acquisition sessions...")

            try:
                from acquisition.models import AcquisitionSession
                from celery.result import AsyncResult

                # Find all running sessions
                running_sessions = AcquisitionSession.objects.filter(
                    status=AcquisitionSession.STATUS_RUNNING
                )

                if running_sessions.exists():
                    logger.info(f"Stopping {running_sessions.count()} running sessions")

                    for session in running_sessions:
                        try:
                            # Revoke the Celery task
                            if session.celery_task_id:
                                celery_task = AsyncResult(session.celery_task_id)
                                celery_task.revoke(terminate=True)
                                logger.info(f"Revoked Celery task {session.celery_task_id} for session {session.id}")

                            # Note: We intentionally keep status as 'running'
                            # so that the session can be recovered on restart
                            logger.info(f"Session {session.id} will be recovered on next startup")

                        except Exception as e:
                            logger.error(f"Error stopping session {session.id}: {e}")

                    logger.info("All acquisition sessions stopped")
                else:
                    logger.info("No running sessions to stop")

            except Exception as e:
                logger.error(f"Error during shutdown: {e}", exc_info=True)

            # Call the original handler if it exists
            if hasattr(shutdown_handler, 'original_handler'):
                original = shutdown_handler.original_handler
                if callable(original):
                    original(signum, frame)

        # Register handlers for SIGTERM and SIGINT
        shutdown_handler.original_handler = signal.signal(signal.SIGTERM, shutdown_handler)
        signal.signal(signal.SIGINT, shutdown_handler)

        logger.info("Registered shutdown handlers for graceful acquisition task termination")


def timezone_now():
    """Wrapper that defers django.utils.timezone import until apps are ready."""
    from django.utils import timezone
    return timezone.now()
