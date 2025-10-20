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


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def start_acquisition_task(self, task_id: int, config_version_id: int = None) -> Dict[str, Any]:
    """
    Start continuous data acquisition for a task.

    Args:
        task_id: ID of the AcqTask to execute
        config_version_id: Optional specific configuration version

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

        # Create acquisition session
        session = acq_models.AcquisitionSession.objects.create(
            task=task,
            status=acq_models.AcquisitionSession.STATUS_RUNNING,
            celery_task_id=self.request.id,
            started_at=timezone.now(),
        )

        try:
            # Use acquisition service to run the task
            service = AcquisitionService(task, session)
            result = service.run_continuous()

            # Update session status
            session.status = acq_models.AcquisitionSession.STATUS_STOPPED
            session.stopped_at = timezone.now()
            session.save(update_fields=["status", "stopped_at", "updated_at"])

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

        # Revoke the celery task if it's running
        if session.celery_task_id:
            from celery import current_app
            current_app.control.revoke(session.celery_task_id, terminate=True)

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
def acquire_once(task_id: int) -> Dict[str, Any]:
    """
    Perform a single acquisition cycle for a task.

    Useful for testing or one-time data collection.

    Args:
        task_id: ID of the AcqTask

    Returns:
        Dict with acquisition results
    """
    logger.info(f"Performing single acquisition for task {task_id}")

    try:
        task = config_models.AcqTask.objects.prefetch_related(
            "points__device",
            "points__template"
        ).get(pk=task_id)

        # Create temporary session
        session = acq_models.AcquisitionSession.objects.create(
            task=task,
            status=acq_models.AcquisitionSession.STATUS_RUNNING,
            started_at=timezone.now(),
        )

        service = AcquisitionService(task, session)
        result = service.acquire_once()

        # Update session
        session.status = acq_models.AcquisitionSession.STATUS_STOPPED
        session.stopped_at = timezone.now()
        session.metadata = {"single_acquisition": True, "points_read": len(result.get("data", []))}
        session.save()

        logger.info(f"Single acquisition for task {task_id} completed")
        return result

    except config_models.AcqTask.DoesNotExist:
        logger.error(f"Task {task_id} does not exist")
        return {"status": "error", "error": "Task not found"}
    except Exception as e:
        logger.error(f"Single acquisition failed: {e}", exc_info=True)
        return {"status": "error", "error": str(e)}


@shared_task
def test_protocol_connection(protocol_type: str, device_config: Dict[str, Any]) -> Dict[str, Any]:
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
def test_storage_connection(storage_type: str, storage_config: Dict[str, Any]) -> Dict[str, Any]:
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
