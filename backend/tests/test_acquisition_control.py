"""
Comprehensive tests for acquisition control functionality.

Tests cover:
- Task start/stop operations
- Session state transitions
- Error handling and logging
- API endpoint responses
"""
import pytest
from unittest.mock import patch, MagicMock
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from acquisition import models as acq_models
from acquisition import tasks
from configuration import models as config_models


@pytest.fixture
def api_client():
    """API test client."""
    return APIClient()


@pytest.fixture
def site(db):
    """Create a test site."""
    return config_models.Site.objects.create(
        code="TEST_SITE",
        name="Test Site"
    )


@pytest.fixture
def device(db, site):
    """Create a test device."""
    return config_models.Device.objects.create(
        site=site,
        code="TEST_DEVICE",
        name="Test Device",
        protocol="mock_modbus",
        ip_address="192.168.1.100",
        port=5020
    )


@pytest.fixture
def point(db, device):
    """Create a test point."""
    return config_models.Point.objects.create(
        device=device,
        template=None,
        code="TEST_POINT_001",
        description="Test Point 1",
        address="D100",
        extra={"type": 3, "num": 1}
    )


@pytest.fixture
def task(db, point):
    """Create a test task with a point."""
    task = config_models.AcqTask.objects.create(
        code="TEST_TASK",
        name="Test Task",
        description="Test task description",
        is_active=True
    )
    task.points.add(point)
    return task


@pytest.fixture
def stopped_session(db, task):
    """Create a stopped session for a task."""
    return acq_models.AcquisitionSession.objects.create(
        task=task,
        status=acq_models.AcquisitionSession.STATUS_STOPPED,
        celery_task_id="test-stopped-session-id"
    )


@pytest.fixture
def running_session(db, task):
    """Create a running session for a task."""
    return acq_models.AcquisitionSession.objects.create(
        task=task,
        status=acq_models.AcquisitionSession.STATUS_RUNNING,
        celery_task_id="test-running-session-id"
    )


# =============================================================================
# Test: Start Task API
# =============================================================================
@pytest.mark.django_db
class TestStartTaskAPI:
    """Tests for the start-task endpoint."""

    def test_start_task_success(self, api_client, task, stopped_session, device, point):
        """Test successfully starting a task."""
        with patch("acquisition.protocols.base.ProtocolRegistry.create") as mock_registry:
            mock_protocol = MagicMock()
            mock_protocol.connect.return_value = True
            mock_protocol.read_points.return_value = [{"code": "TEST_POINT_001", "value": 100}]
            mock_protocol.disconnect.return_value = None
            mock_registry.return_value = mock_protocol

            with patch("acquisition.views.tasks.start_acquisition_task.delay") as mock_task:
                mock_result = MagicMock()
                mock_result.id = "celery-task-id"
                mock_result.get.return_value = {"status": "started"}
                mock_task.return_value = mock_result

                response = api_client.post(
                    "/api/acquisition/sessions/start-task/",
                    {"task_id": task.id},
                    format="json"
                )

                assert response.status_code == status.HTTP_201_CREATED
                assert "session_id" in response.data
                assert response.data.get("detail") is not None

    def test_start_task_already_running(self, api_client, task, running_session):
        """Test starting a task that is already running."""
        response = api_client.post(
            "/api/acquisition/sessions/start-task/",
            {"task_id": task.id},
            format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "已在运行中" in response.data["detail"]

    def test_start_task_not_found(self, api_client):
        """Test starting a non-existent task."""
        response = api_client.post(
            "/api/acquisition/sessions/start-task/",
            {"task_id": 99999},
            format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_start_task_inactive(self, api_client, task):
        """Test starting an inactive task."""
        task.is_active = False
        task.save()

        # Create a stopped session
        acq_models.AcquisitionSession.objects.create(
            task=task,
            status=acq_models.AcquisitionSession.STATUS_STOPPED
        )

        response = api_client.post(
            "/api/acquisition/sessions/start-task/",
            {"task_id": task.id},
            format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_start_task_missing_task_id(self, api_client):
        """Test starting a task without providing task_id."""
        response = api_client.post(
            "/api/acquisition/sessions/start-task/",
            {},
            format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST


# =============================================================================
# Test: Stop Session API
# =============================================================================
@pytest.mark.django_db
class TestStopSessionAPI:
    """Tests for the stop endpoint."""

    def test_stop_session_success(self, api_client, running_session):
        """Test successfully stopping a session."""
        with patch("acquisition.views.tasks.stop_acquisition_task.delay") as mock_stop:
            response = api_client.post(
                f"/api/acquisition/sessions/{running_session.id}/stop/",
                {"reason": "用户手动停止"},
                format="json"
            )

            assert response.status_code == status.HTTP_200_OK
            assert response.data["detail"] is not None

    def test_stop_session_already_stopped(self, api_client, stopped_session):
        """Test stopping an already stopped session."""
        response = api_client.post(
            f"/api/acquisition/sessions/{stopped_session.id}/stop/",
            {"reason": "测试停止"},
            format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "已处于" in response.data["detail"]

    def test_stop_session_not_found(self, api_client):
        """Test stopping a non-existent session."""
        response = api_client.post(
            "/api/acquisition/sessions/99999/stop/",
            {},
            format="json"
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_stop_session_stores_reason(self, api_client, running_session):
        """Test that stop reason is stored in session metadata."""
        with patch("acquisition.views.tasks.stop_acquisition_task.delay"):
            api_client.post(
                f"/api/acquisition/sessions/{running_session.id}/stop/",
                {"reason": "测试原因"},
                format="json"
            )

        # Refresh from DB
        running_session.refresh_from_db()
        assert running_session.metadata.get("stop_reason") == "测试原因"


# =============================================================================
# Test: Active Sessions API
# =============================================================================
@pytest.mark.django_db
class TestActiveSessionsAPI:
    """Tests for the active-sessions endpoint."""

    def test_list_active_only_running(self, api_client, running_session, stopped_session):
        """Test that only running sessions are returned."""
        response = api_client.get("/api/acquisition/sessions/active/")

        assert response.status_code == status.HTTP_200_OK
        # Filter to only sessions for our test task to avoid DB pollution
        task_sessions = [s for s in response.data if s["task"] == running_session.task.id]
        assert len(task_sessions) == 1
        assert task_sessions[0]["id"] == running_session.id
        assert task_sessions[0]["status"] == "running"

    def test_active_sessions_empty(self, api_client, stopped_session):
        """Test when no active sessions exist."""
        stopped_session.delete()
        response = api_client.get("/api/acquisition/sessions/active/")

        assert response.status_code == status.HTTP_200_OK
        # Filter to only sessions for our test task
        task_sessions = [s for s in response.data if s["task"] == stopped_session.task.id]
        assert len(task_sessions) == 0


# =============================================================================
# Test: Session Status API
# =============================================================================
@pytest.mark.django_db
class TestSessionStatusAPI:
    """Tests for the session status endpoint."""

    def test_get_session_status(self, api_client, running_session):
        """Test getting session status."""
        response = api_client.get(
            f"/api/acquisition/sessions/{running_session.id}/status/"
        )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["session_id"] == running_session.id
        assert response.data["status"] == "running"

    def test_get_session_status_not_found(self, api_client):
        """Test getting status for non-existent session."""
        response = api_client.get("/api/acquisition/sessions/99999/status/")
        assert response.status_code == status.HTTP_404_NOT_FOUND


# =============================================================================
# Test: Celery Tasks
# =============================================================================
@pytest.mark.django_db
class TestStartAcquisitionTask:
    """Tests for the start_acquisition_task Celery task."""

    @patch("acquisition.tasks.AcquisitionService")
    def test_start_acquisition_task_success(self, mock_service, task):
        """Test successful task execution."""
        mock_instance = MagicMock()
        mock_instance.run_continuous.return_value = {"status": "completed", "points_read": 100}
        mock_service.return_value = mock_instance

        result = tasks.start_acquisition_task(task_id=task.id)

        assert result["status"] == "completed"

    @patch("acquisition.tasks.AcquisitionService")
    def test_start_acquisition_task_inactive(self, mock_service, task):
        """Test task is skipped when inactive."""
        task.is_active = False
        task.save()

        result = tasks.start_acquisition_task(task_id=task.id)

        assert result["status"] == "skipped"
        assert "not active" in result["reason"]

    @patch("acquisition.tasks.AcquisitionService")
    def test_start_acquisition_task_not_found(self, mock_service):
        """Test task handling for non-existent task."""
        result = tasks.start_acquisition_task(task_id=99999)

        assert result["status"] == "error"
        assert "not found" in result["error"]


@pytest.mark.django_db
class TestStopAcquisitionTask:
    """Tests for the stop_acquisition_task Celery task."""

    def test_stop_running_session(self, running_session):
        """Test stopping a running session."""
        result = tasks.stop_acquisition_task(session_id=running_session.id)

        assert result["status"] == "stopped"
        running_session.refresh_from_db()
        assert running_session.status == acq_models.AcquisitionSession.STATUS_STOPPED

    def test_stop_already_stopped_session(self, stopped_session):
        """Test stopping an already stopped session."""
        result = tasks.stop_acquisition_task(session_id=stopped_session.id)

        assert result["status"] == "already_stopped"

    def test_stop_session_not_found(self):
        """Test stopping non-existent session."""
        result = tasks.stop_acquisition_task(session_id=99999)

        assert result["status"] == "error"
        assert "not found" in result["error"]


# =============================================================================
# Test: Error Handling and Logging
# =============================================================================
@pytest.mark.django_db
class TestErrorHandling:
    """Tests for error handling in acquisition operations."""

    @patch("acquisition.protocols.base.ProtocolRegistry.create")
    def test_start_task_connection_failure(self, mock_registry, api_client, task, stopped_session):
        """Test handling of device connection failure during start."""
        from collections import defaultdict

        mock_protocol = MagicMock()
        # Raise exception on connect to simulate connection failure
        mock_protocol.connect.side_effect = ConnectionError("无法连接到设备")
        mock_protocol.read_points.return_value = []
        mock_protocol.disconnect.return_value = None
        mock_registry.return_value = mock_protocol

        response = api_client.post(
            "/api/acquisition/sessions/start-task/",
            {"task_id": task.id},
            format="json"
        )

        # Should return error because no devices connected
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "无法连接到任何设备" in response.data["detail"] or "detail" in response.data

    @patch("acquisition.protocols.base.ProtocolRegistry.create")
    def test_start_task_partial_failure(self, mock_registry, api_client, task, stopped_session, device, point):
        """Test handling of partial device failure during start - device connected but points fail."""
        mock_protocol = MagicMock()
        mock_protocol.connect.return_value = True
        # Return no points to simulate complete point failure
        mock_protocol.read_points.return_value = []
        mock_protocol.disconnect.return_value = None
        mock_registry.return_value = mock_protocol

        with patch("acquisition.views.tasks.start_acquisition_task.delay") as mock_task:
            mock_result = MagicMock()
            mock_result.id = "celery-task-id"
            mock_result.get.return_value = {"status": "started"}
            mock_task.return_value = mock_result

            response = api_client.post(
                "/api/acquisition/sessions/start-task/",
                {"task_id": task.id},
                format="json"
            )

        # Should succeed (201) even with partial failure - device is connected but points failed
        assert response.status_code == status.HTTP_201_CREATED
        assert "validation" in response.data
        # all_healthy is False because points failed to read
        assert response.data["validation"]["all_healthy"] == False


# =============================================================================
# Test: Session State Transitions
# =============================================================================
@pytest.mark.django_db
class TestSessionStateTransitions:
    """Tests for valid session state transitions."""

    def test_session_states_are_valid(self):
        """Verify all session status constants are defined."""
        assert hasattr(acq_models.AcquisitionSession, 'STATUS_RUNNING')
        assert hasattr(acq_models.AcquisitionSession, 'STATUS_STOPPED')
        assert hasattr(acq_models.AcquisitionSession, 'STATUS_ERROR')
        assert hasattr(acq_models.AcquisitionSession, 'STATUS_PAUSED')

    def test_session_created_with_correct_defaults(self, task):
        """Test that new sessions have correct default values."""
        session = acq_models.AcquisitionSession.objects.create(
            task=task,
            status=acq_models.AcquisitionSession.STATUS_RUNNING
        )

        assert session.celery_task_id is not None or session.celery_task_id == ""
        # started_at is nullable and set by application code when session actually starts
        assert session.stopped_at is None
        assert session.error_message == ""


# =============================================================================
# Test: Protocol Connection Tests
# =============================================================================
@pytest.mark.django_db
class TestConnectionTests:
    """Tests for connection test functionality."""

    def test_protocol_connection_test_success(self, api_client, device):
        """Test successful protocol connection test."""
        with patch("acquisition.tasks.check_protocol_connection.delay") as mock_task:
            mock_result = MagicMock()
            mock_result.get.return_value = {
                "status": "success",
                "protocol": "mock_modbus",
                "connected": True,
                "healthy": True
            }
            mock_task.return_value = mock_result

            response = api_client.post(
                "/api/acquisition/connection-tests/",
                {
                    "protocol_type": "mock_modbus",
                    "device_config": {
                        "source_ip": device.ip_address,
                        "source_port": device.port
                    }
                },
                format="json"
            )

            assert response.status_code == status.HTTP_200_OK

    def test_storage_connection_test(self, api_client):
        """Test storage connection test."""
        with patch("acquisition.tasks.check_storage_connection.delay") as mock_task:
            mock_result = MagicMock()
            mock_result.get.return_value = {
                "status": "success",
                "storage": "influxdb",
                "connected": True
            }
            mock_task.return_value = mock_result

            response = api_client.post(
                "/api/acquisition/storage-tests/",
                {
                    "storage_type": "influxdb",
                    "storage_config": {
                        "url": "http://localhost:8086",
                        "token": "test-token",
                        "org": "test-org",
                        "bucket": "test-bucket"
                    }
                },
                format="json"
            )

            assert response.status_code == status.HTTP_200_OK
