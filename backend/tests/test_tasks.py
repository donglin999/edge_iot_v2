"""Unit tests for Celery tasks."""
import pytest
from unittest.mock import patch, MagicMock
from django.utils import timezone

from acquisition import models as acq_models
from acquisition.tasks import (
    start_acquisition_task,
    stop_acquisition_task,
    acquire_once,
    check_protocol_connection,
    check_storage_connection,
)
from configuration.tasks import process_excel_import
from tests.fixtures.factories import *
from tests.mocks.protocols import register_mock_protocols
from tests.mocks.storage import register_mock_storage


@pytest.fixture(autouse=True)
def setup_mocks():
    """Setup mock protocols and storage."""
    register_mock_protocols()
    register_mock_storage()
    yield


@pytest.mark.django_db
class TestStartAcquisitionTask:
    """Tests for start_acquisition_task."""

    @patch("acquisition.tasks.AcquisitionService")
    def test_start_acquisition_success(self, mock_service_class, create_task, create_session):
        """Test successful task start."""
        task = create_task(is_active=True)
        mock_service = MagicMock()
        mock_service.run_continuous.return_value = {"status": "completed", "cycles": 10}
        mock_service_class.return_value = mock_service

        # Run task synchronously
        result = start_acquisition_task(task.id)

        assert result["status"] == "completed"
        assert "cycles" in result

        # Session should be stopped
        session = acq_models.AcquisitionSession.objects.filter(task=task).first()
        assert session.status == acq_models.AcquisitionSession.STATUS_STOPPED

    @patch("acquisition.tasks.AcquisitionService")
    def test_start_inactive_task(self, mock_service_class, create_task):
        """Test starting an inactive task."""
        task = create_task(is_active=False)

        result = start_acquisition_task(task.id)

        assert result["status"] == "skipped"
        assert "not active" in result["reason"]

    def test_start_nonexistent_task(self):
        """Test starting a task that doesn't exist."""
        result = start_acquisition_task(99999)

        assert result["status"] == "error"
        assert "not found" in result["error"]

    @patch("acquisition.tasks.AcquisitionService")
    def test_start_task_error_handling(self, mock_service_class, create_task, create_session):
        """Test error handling during acquisition."""
        task = create_task(is_active=True)
        mock_service = MagicMock()
        mock_service.run_continuous.side_effect = Exception("Connection failed")
        mock_service_class.return_value = mock_service

        with pytest.raises(Exception, match="Connection failed"):
            start_acquisition_task(task.id)

        # Session should have error status
        session = acq_models.AcquisitionSession.objects.filter(task=task).first()
        assert session.status == acq_models.AcquisitionSession.STATUS_ERROR
        assert "Connection failed" in session.error_message


@pytest.mark.django_db
class TestStopAcquisitionTask:
    """Tests for stop_acquisition_task."""

    def test_stop_running_session(self, create_session):
        """Test stopping a running session."""
        session = create_session(status="running")

        with patch("celery.current_app") as mock_app:
            result = stop_acquisition_task(session.id)

        assert result["status"] == "stopped"
        session.refresh_from_db()
        assert session.status == acq_models.AcquisitionSession.STATUS_STOPPED

    def test_stop_already_stopped_session(self, create_session):
        """Test stopping an already stopped session."""
        session = create_session(status="stopped")

        result = stop_acquisition_task(session.id)

        assert result["status"] == "already_stopped"

    def test_stop_nonexistent_session(self):
        """Test stopping a session that doesn't exist."""
        result = stop_acquisition_task(99999)

        assert result["status"] == "error"
        assert "not found" in result["error"]


@pytest.mark.django_db
class TestAcquireOnceTask:
    """Tests for acquire_once task.

    The task now delegates straight to ``ProtocolRegistry.create`` /
    ``read_batch`` (no AcquisitionService instantiation), so the tests mock
    the protocol layer instead of the service layer.
    """

    def test_acquire_once_success(self, create_task, create_point, create_device):
        """A successful single read returns status=success and a count."""
        device = create_device(protocol="mock_modbus")
        point = create_point(device=device, code="P1")
        task = create_task(points=[point])

        result = acquire_once(task.id)

        assert result["status"] == "success"
        assert result["count"] >= 1
        assert any(r["point_code"] == "P1" for r in result["readings"])

    def test_acquire_once_nonexistent_task(self):
        """Test single acquisition for nonexistent task."""
        result = acquire_once(99999)

        assert result["status"] == "error"
        assert "not found" in result["error"]

    def test_acquire_once_error(self, create_task, create_point, create_device):
        """A protocol read failure surfaces as status=error with the message."""
        device = create_device(
            protocol="mock_modbus",
            metadata={"_test_read_fail": True},
        )
        point = create_point(device=device, code="P1")
        task = create_task(points=[point])

        result = acquire_once(task.id)

        assert result["status"] == "error"
        assert "Simulated read failure" in result["error"]
        assert result["device"] == device.code


@pytest.mark.django_db
class TestTestProtocolConnectionTask:
    """Tests for check_protocol_connection task."""

    def test_test_protocol_connection_success(self):
        """Test successful protocol connection test."""
        config = {
            "source_ip": "192.168.1.100",
            "source_port": 502,
            "_test_simulated_data": {"P1": 100},
        }

        result = check_protocol_connection("mock_modbus", config)

        assert result["status"] == "success"
        assert result["protocol"] == "mock_modbus"
        assert result["connected"] is True

    def test_test_protocol_connection_failure(self):
        """Test failed protocol connection."""
        config = {
            "source_ip": "192.168.1.100",
            "source_port": 502,
            "_test_connection_fail": True,
        }

        result = check_protocol_connection("mock_modbus", config)

        assert result["status"] == "unhealthy"

    def test_test_unknown_protocol(self):
        """Test unknown protocol."""
        config = {"source_ip": "192.168.1.100", "source_port": 502}

        result = check_protocol_connection("unknown_protocol", config)

        assert result["status"] == "error"
        assert "not registered" in result["error"]


@pytest.mark.django_db
class TestTestStorageConnectionTask:
    """Tests for check_storage_connection task."""

    def test_test_storage_connection_success(self):
        """Test successful storage connection test."""
        config = {
            "host": "localhost",
            "port": 8086,
            "token": "test",
            "org": "test",
            "bucket": "test",
        }

        result = check_storage_connection("mock_influxdb", config)

        assert result["status"] == "success"
        assert result["storage"] == "mock_influxdb"
        assert result["connected"] is True

    def test_test_storage_connection_failure(self):
        """Test failed storage connection."""
        config = {
            "host": "localhost",
            "port": 8086,
            "_test_connect_fail": True,
        }

        result = check_storage_connection("mock_influxdb", config)

        assert result["status"] == "unhealthy"

    def test_test_unknown_storage(self):
        """Test unknown storage."""
        config = {"host": "localhost"}

        result = check_storage_connection("unknown_storage", config)

        assert result["status"] == "error"


@pytest.mark.django_db
class TestProcessExcelImportTask:
    """Tests for process_excel_import task."""

    @patch("configuration.tasks.importer.process_excel")
    def test_process_excel_import_success(self, mock_process, create_import_job, tmp_path):
        """Test successful Excel import processing."""
        # Create a real Excel file
        import pandas as pd
        excel_path = tmp_path / "test.xlsx"
        pd.DataFrame({
            "protocol_type": ["modbus_tcp"],
            "source_ip": ["192.168.1.100"],
            "source_port": [502],
            "en_name": ["P1"],
        }).to_excel(excel_path, index=False)

        job = create_import_job()
        mock_process.return_value = MagicMock(
            rows_parsed=1,
            is_successful=True,
            to_dict=lambda: {"rows_parsed": 1, "is_successful": True},
        )

        result = process_excel_import(job.id, str(excel_path), site_code="test_site")

        assert "rows_parsed" in result

    def test_process_excel_import_job_not_found(self):
        """Test processing with nonexistent job."""
        from configuration.models import ImportJob

        with pytest.raises(ImportJob.DoesNotExist):
            process_excel_import(99999, "/tmp/test.xlsx")
