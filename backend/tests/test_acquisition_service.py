"""Unit tests for acquisition service layer."""
import pytest
from unittest.mock import patch, MagicMock

from acquisition.services.acquisition_service import AcquisitionService
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
class TestAcquisitionService:
    """Test AcquisitionService functionality."""

    def test_service_initialization(self, create_task, create_session):
        """Test service initialization."""
        task = create_task()
        session = create_session(task=task)

        service = AcquisitionService(task, session)

        assert service.task == task
        assert service.session == session
        assert len(service.device_groups) > 0

    def test_group_points_by_device(self, create_task, create_device, create_point):
        """Test grouping points by device."""
        # Create 2 devices with points
        device1 = create_device(code="DEV_001", protocol="mock_modbus")
        device2 = create_device(code="DEV_002", protocol="mock_plc")

        point1 = create_point(device=device1, code="P1")
        point2 = create_point(device=device1, code="P2")
        point3 = create_point(device=device2, code="P3")

        task = create_task(points=[point1, point2, point3])
        session = create_session(task=task)

        service = AcquisitionService(task, session)

        # Should have 2 device groups
        assert len(service.device_groups) == 2

        # Check first device group
        group1 = service.device_groups[device1.id]
        assert group1["device"] == device1
        assert len(group1["points"]) == 2

        # Check second device group
        group2 = service.device_groups[device2.id]
        assert group2["device"] == device2
        assert len(group2["points"]) == 1

    @patch("acquisition.services.acquisition_service.settings")
    def test_acquire_once_success(
        self,
        mock_settings,
        create_task,
        create_session,
        create_device,
        create_point,
    ):
        """Test successful single acquisition."""
        # Setup mock settings
        mock_settings.INFLUXDB_HOST = "localhost"
        mock_settings.INFLUXDB_PORT = 8086
        mock_settings.INFLUXDB_TOKEN = "test-token"
        mock_settings.INFLUXDB_ORG = "test-org"
        mock_settings.INFLUXDB_BUCKET = "test-bucket"
        mock_settings.KAFKA_ENABLED = False

        # Create test data
        device = create_device(
            protocol="mock_modbus",
            ip="192.168.1.100",
            metadata={"_test_simulated_data": {"P1": 100, "P2": 200}},
        )
        point1 = create_point(device=device, code="P1", address="D100")
        point2 = create_point(device=device, code="P2", address="D101")

        task = create_task(points=[point1, point2])
        session = create_session(task=task)

        # Patch storage registry to use mock
        with patch("acquisition.services.acquisition_service.StorageRegistry") as mock_registry:
            mock_storage = MagicMock()
            mock_storage.connect.return_value = True
            mock_storage.write.return_value = True
            mock_registry.create.return_value = mock_storage

            service = AcquisitionService(task, session)
            result = service.acquire_once()

        # Verify result
        assert result["status"] == "completed"
        assert result["points_read"] == 2
        assert len(result["errors"]) == 0

        # Verify storage was called
        mock_storage.connect.assert_called()
        mock_storage.write.assert_called()

    @patch("acquisition.services.acquisition_service.settings")
    def test_acquire_once_with_error(
        self,
        mock_settings,
        create_task,
        create_session,
        create_device,
        create_point,
    ):
        """Test acquisition with protocol error."""
        # Setup
        mock_settings.INFLUXDB_HOST = "localhost"
        mock_settings.INFLUXDB_PORT = 8086
        mock_settings.INFLUXDB_TOKEN = "test-token"
        mock_settings.INFLUXDB_ORG = "test-org"
        mock_settings.INFLUXDB_BUCKET = "test-bucket"
        mock_settings.KAFKA_ENABLED = False

        # Create device that will fail
        device = create_device(
            protocol="mock_modbus",
            metadata={"_test_connection_fail": True},
        )
        point = create_point(device=device, code="P1")

        task = create_task(points=[point])
        session = create_session(task=task)

        with patch("acquisition.services.acquisition_service.StorageRegistry") as mock_registry:
            mock_storage = MagicMock()
            mock_registry.create.return_value = mock_storage

            service = AcquisitionService(task, session)
            result = service.acquire_once()

        # Should have errors
        assert len(result["errors"]) > 0
        assert result["points_read"] == 0

    def test_format_for_storage(
        self,
        create_task,
        create_session,
        create_device,
        create_point,
        create_point_template,
    ):
        """Test data formatting for storage."""
        template = create_point_template(name="temperature", unit="°C")
        device = create_device(code="TEST_DEV", metadata={"device_a_tag": "SENSOR_001"})
        point = create_point(device=device, code="TEMP_01", template=template)

        task = create_task(points=[point])
        session = create_session(task=task)

        service = AcquisitionService(task, session)

        # Mock readings
        readings = [
            {
                "code": "TEMP_01",
                "value": 25.5,
                "timestamp": 1234567890000000000,
                "quality": "good",
            }
        ]

        formatted = service._format_for_storage(readings, device)

        assert len(formatted) == 1
        assert formatted[0]["measurement"] == "SENSOR_001"
        assert formatted[0]["tags"]["device"] == "TEST_DEV"
        assert formatted[0]["tags"]["point"] == "TEMP_01"
        assert formatted[0]["tags"]["cn_name"] == template.name
        assert formatted[0]["tags"]["unit"] == "°C"
        assert formatted[0]["fields"]["TEMP_01"] == 25.5
        assert formatted[0]["time"] == 1234567890000000000

    @patch("acquisition.services.acquisition_service.settings")
    def test_write_to_storage(
        self,
        mock_settings,
        create_task,
        create_session,
    ):
        """Test writing data to storage."""
        mock_settings.INFLUXDB_HOST = "localhost"
        mock_settings.KAFKA_ENABLED = False

        task = create_task()
        session = create_session(task=task)

        with patch("acquisition.services.acquisition_service.StorageRegistry") as mock_registry:
            mock_storage = MagicMock()
            mock_storage.write.return_value = True
            mock_registry.create.return_value = mock_storage

            service = AcquisitionService(task, session)

            data = [
                {
                    "measurement": "test",
                    "tags": {"site": "test"},
                    "fields": {"value": 1},
                }
            ]

            service._write_to_storage(data)

            # Verify write was called
            mock_storage.write.assert_called_once_with(data)

    def test_should_continue_running(self, create_task, create_session):
        """Test acquisition continuation check."""
        task = create_task()
        session = create_session(task=task, status="running")

        with patch("acquisition.services.acquisition_service.settings"):
            service = AcquisitionService(task, session)

            # Should continue when running
            assert service._should_continue()

            # Should stop when marked as stopping
            session.status = "stopping"
            session.save()
            assert not service._should_continue()

            # Should stop when stopped
            session.status = "stopped"
            session.save()
            assert not service._should_continue()

    def test_get_cycle_interval(self, create_task, create_session):
        """Test acquisition cycle interval."""
        task = create_task(schedule="continuous")
        session = create_session(task=task)

        with patch("acquisition.services.acquisition_service.settings"):
            service = AcquisitionService(task, session)

            interval = service._get_cycle_interval()
            assert interval == 1.0  # Default for continuous


@pytest.mark.django_db
class TestStorageInitialization:
    """Test storage backend initialization."""

    @patch("acquisition.services.acquisition_service.settings")
    @patch("acquisition.services.acquisition_service.StorageRegistry")
    def test_influxdb_initialization_success(
        self,
        mock_registry,
        mock_settings,
        create_task,
        create_session,
    ):
        """Test successful InfluxDB initialization."""
        mock_settings.INFLUXDB_HOST = "localhost"
        mock_settings.INFLUXDB_PORT = 8086
        mock_settings.INFLUXDB_TOKEN = "test-token"
        mock_settings.INFLUXDB_ORG = "test-org"
        mock_settings.INFLUXDB_BUCKET = "test-bucket"
        mock_settings.KAFKA_ENABLED = False

        mock_storage = MagicMock()
        mock_storage.connect.return_value = True
        mock_registry.create.return_value = mock_storage

        task = create_task()
        session = create_session(task=task)

        service = AcquisitionService(task, session)

        # Should have InfluxDB storage
        assert "influxdb" in service.storages
        mock_registry.create.assert_called()

    @patch("acquisition.services.acquisition_service.settings")
    @patch("acquisition.services.acquisition_service.StorageRegistry")
    def test_storage_initialization_failure(
        self,
        mock_registry,
        mock_settings,
        create_task,
        create_session,
    ):
        """Test handling of storage initialization failure."""
        mock_settings.INFLUXDB_HOST = "localhost"
        mock_settings.KAFKA_ENABLED = False

        # Make storage creation fail
        mock_registry.create.side_effect = Exception("Connection failed")

        task = create_task()
        session = create_session(task=task)

        # Should not raise exception, just log warning
        service = AcquisitionService(task, session)

        # No storages should be initialized
        assert len(service.storages) == 0
