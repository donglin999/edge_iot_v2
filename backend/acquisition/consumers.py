"""WebSocket consumers for real-time acquisition updates."""
import json
import logging
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async

logger = logging.getLogger(__name__)


class AcquisitionConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for real-time acquisition session updates.

    Usage:
        ws://localhost:8000/ws/acquisition/sessions/{session_id}/
    """

    async def connect(self):
        """Handle WebSocket connection."""
        self.session_id = self.scope['url_route']['kwargs']['session_id']
        self.session_group_name = f'acquisition_session_{self.session_id}'

        # Join session group
        await self.channel_layer.group_add(
            self.session_group_name,
            self.channel_name
        )

        await self.accept()

        logger.info(f"WebSocket connected for session {self.session_id}")

        # Send initial status
        try:
            session_data = await self.get_session_status()
            await self.send(text_data=json.dumps({
                'type': 'session_status',
                'data': session_data
            }))
        except Exception as e:
            logger.error(f"Failed to send initial status: {e}")

    async def disconnect(self, close_code):
        """Handle WebSocket disconnection."""
        # Leave session group
        await self.channel_layer.group_discard(
            self.session_group_name,
            self.channel_name
        )

        logger.info(f"WebSocket disconnected for session {self.session_id}, code={close_code}")

    async def receive(self, text_data):
        """Handle messages from WebSocket (not used in this implementation)."""
        pass

    async def session_status_update(self, event):
        """
        Handle session status update from channel layer.

        Called when a message is sent to the session group via:
            channel_layer.group_send('acquisition_session_123', {
                'type': 'session_status_update',
                'data': {...}
            })
        """
        await self.send(text_data=json.dumps({
            'type': 'session_status',
            'data': event['data']
        }))

    async def data_point_update(self, event):
        """
        Handle new data point from channel layer.

        Called when a new data point is acquired.
        """
        await self.send(text_data=json.dumps({
            'type': 'data_point',
            'data': event['data']
        }))

    async def session_error(self, event):
        """Handle session error notification."""
        await self.send(text_data=json.dumps({
            'type': 'error',
            'data': event['data']
        }))

    @database_sync_to_async
    def get_session_status(self):
        """Fetch current session status from database."""
        from acquisition import models as acq_models
        from django.core.exceptions import ObjectDoesNotExist

        try:
            session = acq_models.AcquisitionSession.objects.select_related('task').get(
                pk=self.session_id
            )

            # Count data points
            data_points_count = acq_models.DataPoint.objects.filter(
                session=session
            ).count()

            error_count = acq_models.DataPoint.objects.filter(
                session=session,
                quality__in=['bad', 'uncertain']
            ).count()

            last_data_point = acq_models.DataPoint.objects.filter(
                session=session
            ).order_by('-timestamp').first()

            duration_seconds = None
            if session.started_at:
                from django.utils import timezone
                end_time = session.stopped_at or timezone.now()
                duration_seconds = (end_time - session.started_at).total_seconds()

            return {
                'session_id': session.id,
                'task_code': session.task.code,
                'task_name': session.task.name,
                'status': session.status,
                'started_at': session.started_at.isoformat() if session.started_at else None,
                'stopped_at': session.stopped_at.isoformat() if session.stopped_at else None,
                'duration_seconds': duration_seconds,
                'points_read': data_points_count,
                'last_read_time': last_data_point.timestamp.isoformat() if last_data_point else None,
                'error_count': error_count,
                'error_message': session.error_message,
            }

        except ObjectDoesNotExist:
            return {
                'error': 'Session not found',
                'session_id': self.session_id
            }


class GlobalAcquisitionConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for global acquisition updates (all sessions).

    Usage:
        ws://localhost:8000/ws/acquisition/global/
    """

    async def connect(self):
        """Handle WebSocket connection."""
        self.group_name = 'acquisition_global'

        # Join global group
        await self.channel_layer.group_add(
            self.group_name,
            self.channel_name
        )

        await self.accept()

        logger.info("WebSocket connected for global acquisition updates")

        # Send initial status of all active sessions
        try:
            active_sessions = await self.get_active_sessions()
            await self.send(text_data=json.dumps({
                'type': 'active_sessions',
                'data': active_sessions
            }))
        except Exception as e:
            logger.error(f"Failed to send initial active sessions: {e}")

    async def disconnect(self, close_code):
        """Handle WebSocket disconnection."""
        await self.channel_layer.group_discard(
            self.group_name,
            self.channel_name
        )

        logger.info(f"WebSocket disconnected from global updates, code={close_code}")

    async def receive(self, text_data):
        """Handle messages from WebSocket."""
        pass

    async def session_status_update(self, event):
        """Handle any session status update."""
        await self.send(text_data=json.dumps({
            'type': 'session_status',
            'data': event['data']
        }))

    async def session_started(self, event):
        """Handle session start notification."""
        await self.send(text_data=json.dumps({
            'type': 'session_started',
            'data': event['data']
        }))

    async def session_stopped(self, event):
        """Handle session stop notification."""
        await self.send(text_data=json.dumps({
            'type': 'session_stopped',
            'data': event['data']
        }))

    @database_sync_to_async
    def get_active_sessions(self):
        """Fetch all active sessions."""
        from acquisition import models as acq_models

        sessions = acq_models.AcquisitionSession.objects.filter(
            status__in=[
                acq_models.AcquisitionSession.STATUS_RUNNING,
                acq_models.AcquisitionSession.STATUS_RUNNING,
            ]
        ).select_related('task')[:50]  # Limit to 50 most recent

        return [{
            'session_id': s.id,
            'task_id': s.task.id,
            'task_code': s.task.code,
            'status': s.status,
            'started_at': s.started_at.isoformat() if s.started_at else None,
        } for s in sessions]
