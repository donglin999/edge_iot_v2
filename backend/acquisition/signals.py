"""Django signals for sending WebSocket notifications."""
import logging
from django.db.models.signals import post_save
from django.dispatch import receiver
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync

from acquisition import models as acq_models

logger = logging.getLogger(__name__)


@receiver(post_save, sender=acq_models.AcquisitionSession)
def session_status_changed(sender, instance, created, **kwargs):
    """
    Send WebSocket notification when session status changes.

    This is triggered whenever an AcquisitionSession is saved.
    """
    channel_layer = get_channel_layer()
    if not channel_layer:
        logger.warning("Channel layer not available, skipping WebSocket notification")
        return

    session_data = {
        'id': instance.id,
        'session_id': instance.id,
        'task': instance.task.id,
        'task_id': instance.task.id,
        'task_code': instance.task.code,
        'task_name': instance.task.name,
        'status': instance.status,
        'started_at': instance.started_at.isoformat() if instance.started_at else None,
        'stopped_at': instance.stopped_at.isoformat() if instance.stopped_at else None,
        'error_message': instance.error_message,
    }

    # Send to session-specific group
    session_group = f'acquisition_session_{instance.id}'
    try:
        async_to_sync(channel_layer.group_send)(
            session_group,
            {
                'type': 'session_status_update',
                'data': session_data,
            }
        )
        logger.debug(f"Sent status update to {session_group}")
    except Exception as e:
        logger.error(f"Failed to send WebSocket notification to {session_group}: {e}")

    # Send to global group
    global_group = 'acquisition_global'
    try:
        if created:
            # New session started
            async_to_sync(channel_layer.group_send)(
                global_group,
                {
                    'type': 'session_started',
                    'data': session_data,
                }
            )
        elif instance.status in [acq_models.AcquisitionSession.STATUS_STOPPED,
                                  acq_models.AcquisitionSession.STATUS_ERROR]:
            # Session stopped or errored
            async_to_sync(channel_layer.group_send)(
                global_group,
                {
                    'type': 'session_stopped',
                    'data': session_data,
                }
            )
        else:
            # General status update
            async_to_sync(channel_layer.group_send)(
                global_group,
                {
                    'type': 'session_status_update',
                    'data': session_data,
                }
            )
        logger.debug(f"Sent status update to {global_group}")
    except Exception as e:
        logger.error(f"Failed to send WebSocket notification to {global_group}: {e}")


# Note: the legacy ``data_point_created`` signal that listened on
# ``acq_models.DataPoint`` was removed — the DataPoint table is no longer
# written to by the pipeline. Real-time WebSocket pushes happen from the
# WebSocketSink (see ``services/sinks.py``) instead, with a 1 Hz aggregation
# window to keep traffic bounded at high sample rates.
