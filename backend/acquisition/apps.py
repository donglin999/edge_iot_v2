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

    def ready(self):
        """Import signals and recover running acquisition sessions."""
        import acquisition.signals  # noqa

        # Only run recovery in main process (not in runserver reloader)
        import os
        if os.environ.get('RUN_MAIN') != 'true':
            return

        # Register shutdown handler
        self._register_shutdown_handler()

        # Recover running sessions after Django startup
        self._recover_sessions()

    def _recover_sessions(self):
        """
        Recover running acquisition sessions after Django restart.

        This method:
        1. Finds all sessions that were marked as 'running' before restart
        2. Restarts the corresponding Celery tasks
        3. Maintains system consistency between Django and Celery
        """
        try:
            # Import here to avoid AppRegistryNotReady errors
            from acquisition.models import AcquisitionSession
            from acquisition import tasks

            # Find all sessions that were running before shutdown
            running_sessions = AcquisitionSession.objects.filter(
                status=AcquisitionSession.STATUS_RUNNING
            )

            if not running_sessions.exists():
                logger.info("No running sessions to recover")
                return

            logger.info(f"Found {running_sessions.count()} running sessions to recover")

            for session in running_sessions:
                try:
                    logger.info(f"Recovering session {session.id} for task {session.task.code}")

                    # Cancel the old Celery task if it exists
                    if session.celery_task_id:
                        from celery.result import AsyncResult
                        old_task = AsyncResult(session.celery_task_id)
                        try:
                            old_task.revoke(terminate=True)
                        except Exception as e:
                            logger.warning(f"Failed to revoke old task {session.celery_task_id}: {e}")

                    # Delete the old session and start a fresh task
                    # (start_acquisition_task will create a new session)
                    task_id = session.task.id
                    task_code = session.task.code

                    with transaction.atomic():
                        session.delete()

                    # Start a new Celery task with task_id (not session_id)
                    celery_task = tasks.start_acquisition_task.delay(task_id)

                    logger.info(f"Successfully recovered task {task_code} (task_id={task_id}) with new Celery task {celery_task.id}")

                except Exception as e:
                    logger.error(f"Failed to recover session {session.id}: {e}", exc_info=True)
                    # Mark session as error but don't stop recovery of other sessions
                    try:
                        with transaction.atomic():
                            session.status = AcquisitionSession.STATUS_ERROR
                            session.error_message = f"Failed to recover after restart: {str(e)}"
                            session.save(update_fields=['status', 'error_message', 'updated_at'])
                    except Exception as save_err:
                        logger.error(f"Failed to update session {session.id} status: {save_err}")

            logger.info("Session recovery completed")

        except Exception as e:
            logger.error(f"Session recovery failed: {e}", exc_info=True)

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
