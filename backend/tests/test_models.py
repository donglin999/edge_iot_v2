"""Unit tests for configuration and acquisition models."""
import pytest
from django.db import IntegrityError

from acquisition import models as acq_models
from configuration import models as config_models
from tests.fixtures.factories import *


@pytest.mark.django_db
class TestSiteModel:
    """Tests for Site model."""

    def test_create_site(self, create_site):
        """Test creating a site."""
        site = create_site(code="TEST_SITE", name="Test Site")
        assert site.code == "TEST_SITE"
        assert site.name == "Test Site"
        assert site.description == "Test site description"

    def test_site_str_representation(self, create_site):
        """Test site string representation."""
        site = create_site(code="SITE_001", name="Factory 1")
        assert str(site) == "SITE_001 - Factory 1"

    def test_site_code_unique(self, create_site):
        """Test that site code must be unique."""
        create_site(code="UNIQUE_SITE")
        with pytest.raises(IntegrityError):
            create_site(code="UNIQUE_SITE")


@pytest.mark.django_db
class TestDeviceModel:
    """Tests for Device model."""

    def test_create_device(self, create_site, create_device):
        """Test creating a device."""
        site = create_site()
        device = create_device(
            site=site,
            code="DEV_001",
            name="PLC Controller",
            protocol="modbus_tcp",
            ip="192.168.1.100",
            port=502
        )
        assert device.code == "DEV_001"
        assert device.name == "PLC Controller"
        assert device.protocol == "modbus_tcp"
        assert device.ip_address == "192.168.1.100"
        assert device.port == 502

    def test_device_str_representation(self, create_site, create_device):
        """Test device string representation."""
        site = create_site(code="SITE_A")
        device = create_device(site=site, code="DEVICE_1")
        assert str(device) == "SITE_A:DEVICE_1"

    def test_device_unique_constraint(self, create_site, create_device):
        """``Device.code`` is the stable identity and must be unique.

        After the schema-driven importer refactor the (site, protocol, ip, port)
        tuple is no longer enforced — non-IP protocols (Modbus RTU, OPC-UA over
        endpoint string, ...) leave ip/port blank, so the unique key moved onto
        ``code`` (composed from each protocol's ``IDENTITY_FIELDS`` by the
        importer). See ``configuration/models.py:Device.code``.
        """
        site = create_site()
        create_device(site=site, code="DUPLICATE_CODE", protocol="modbus_tcp")

        # Same code should violate the unique=True constraint, even on a
        # different site / protocol / ip combination.
        with pytest.raises(IntegrityError):
            create_device(
                site=create_site(),
                code="DUPLICATE_CODE",
                protocol="mqtt",
                ip="10.0.0.1",
                port=1883,
            )

    def test_device_metadata(self, create_site, create_device):
        """Test device metadata field."""
        metadata = {"manufacturer": "Siemens", "model": "S7-1500"}
        device = create_device(metadata=metadata)
        assert device.metadata == metadata


@pytest.mark.django_db
class TestPointTemplateModel:
    """Tests for PointTemplate model."""

    def test_create_template(self, create_point_template):
        """Test creating a point template."""
        template = create_point_template(
            name="temperature_sensor",
            unit="°C",
            data_type="float",
            coefficient="0.1"
        )
        assert template.name == "temperature_sensor"
        assert template.unit == "°C"
        assert template.data_type == "float"
        assert float(template.coefficient) == 0.1


@pytest.mark.django_db
class TestPointModel:
    """Tests for Point model."""

    def test_create_point(self, create_device, create_point):
        """Test creating a point."""
        device = create_device()
        point = create_point(device=device, code="TEMP_001", address="D100")
        assert point.code == "TEMP_001"
        assert point.address == "D100"
        assert point.device == device

    def test_point_str_representation(self, create_site, create_device, create_point):
        """Test point string representation."""
        site = create_site(code="SITE_1")
        device = create_device(site=site, code="DEV_1")
        point = create_point(device=device, code="POINT_A")
        assert str(point) == "SITE_1:DEV_1:POINT_A"

    def test_point_unique_per_device(self, create_device, create_point):
        """Test that point code is unique per device."""
        device = create_device()
        create_point(device=device, code="UNIQUE_POINT")

        with pytest.raises(IntegrityError):
            create_point(device=device, code="UNIQUE_POINT")


@pytest.mark.django_db
class TestAcqTaskModel:
    """Tests for AcqTask model."""

    def test_create_task(self, create_task):
        """Test creating an acquisition task."""
        task = create_task(code="TASK_001", name="Temperature Monitoring")
        assert task.code == "TASK_001"
        assert task.name == "Temperature Monitoring"
        assert task.is_active is True

    def test_task_str_representation(self, create_task):
        """Test task string representation."""
        task = create_task(name="Pressure Monitor")
        assert str(task) == "Pressure Monitor"

    def test_task_with_points(self, create_task, create_point):
        """Test task with associated points."""
        point1 = create_point(code="P1")
        point2 = create_point(code="P2")
        task = create_task(points=[point1, point2])

        assert task.points.count() == 2
        assert point1 in task.points.all()
        assert point2 in task.points.all()

    def test_task_code_unique(self, create_task):
        """Test that task code must be unique."""
        create_task(code="UNIQUE_TASK")
        with pytest.raises(IntegrityError):
            create_task(code="UNIQUE_TASK")


@pytest.mark.django_db
class TestImportJobModel:
    """Tests for ImportJob model."""

    def test_create_import_job(self, create_import_job):
        """Test creating an import job."""
        job = create_import_job(
            source_name="test_data.xlsx",
            status="pending"
        )
        assert job.source_name == "test_data.xlsx"
        assert job.status == "pending"

    def test_import_job_str_representation(self, create_import_job):
        """Test import job string representation."""
        job = create_import_job(source_name="import.xlsx", status="pending")
        assert "pending" in str(job)

    def test_import_job_status_choices(self, create_import_job):
        """Test import job status transitions."""
        job = create_import_job(status=config_models.ImportJob.STATUS_PENDING)
        assert job.status == "pending"

        job.status = config_models.ImportJob.STATUS_VALIDATED
        job.save()

        job.refresh_from_db()
        assert job.status == config_models.ImportJob.STATUS_VALIDATED


@pytest.mark.django_db
class TestWorkerEndpointModel:
    """Tests for WorkerEndpoint model."""

    def test_create_worker(self, create_worker_endpoint):
        """Test creating a worker endpoint."""
        worker = create_worker_endpoint(
            identifier="worker-1",
            host="192.168.1.10"
        )
        assert worker.identifier == "worker-1"
        assert worker.host == "192.168.1.10"
        assert worker.status == "unknown"

    def test_worker_str_representation(self, create_worker_endpoint):
        """Test worker string representation."""
        worker = create_worker_endpoint(identifier="edge-worker-01")
        assert str(worker) == "edge-worker-01"

    def test_worker_identifier_unique(self, create_worker_endpoint):
        """Test that worker identifier must be unique."""
        create_worker_endpoint(identifier="UNIQUE_WORKER")
        with pytest.raises(IntegrityError):
            create_worker_endpoint(identifier="UNIQUE_WORKER")


@pytest.mark.django_db
class TestTaskRunModel:
    """Tests for TaskRun model."""

    def test_create_task_run(self, create_task_run):
        """Test creating a task run."""
        run = create_task_run(status="pending")
        assert run.status == "pending"

    def test_task_run_status_choices(self, create_task_run):
        """Test task run status transitions."""
        run = create_task_run(status=config_models.TaskRun.STATUS_PENDING)
        assert run.status == "pending"

        run.status = config_models.TaskRun.STATUS_RUNNING
        run.save()

        run.refresh_from_db()
        assert run.status == config_models.TaskRun.STATUS_RUNNING


@pytest.mark.django_db
class TestAcquisitionSessionModel:
    """Tests for AcquisitionSession model."""

    def test_create_session(self, create_session):
        """Test creating an acquisition session."""
        session = create_session(status="running")
        assert session.status == "running"

    def test_session_str_representation(self, create_session):
        """Test session string representation."""
        session = create_session()
        assert session.status in str(session)

    def test_session_status_choices(self, create_session):
        """Test session status constants."""
        session = create_session(status=acq_models.AcquisitionSession.STATUS_RUNNING)
        assert session.status == acq_models.AcquisitionSession.STATUS_RUNNING

        session.status = acq_models.AcquisitionSession.STATUS_STOPPED
        session.save()

        session.refresh_from_db()
        assert session.status == acq_models.AcquisitionSession.STATUS_STOPPED

    def test_session_cascade_delete(self, create_task, create_session):
        """Test that sessions are deleted when task is deleted."""
        task = create_task()
        session = create_session(task=task)
        session_id = session.id

        task.delete()

        assert not acq_models.AcquisitionSession.objects.filter(id=session_id).exists()
