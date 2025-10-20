"""Integration tests for end-to-end acquisition pipeline."""
import pytest
from unittest.mock import patch

from acquisition.services.acquisition_service import AcquisitionService
from tests.fixtures.factories import *
from tests.mocks.protocols import register_mock_protocols
from tests.mocks.storage import register_mock_storage, MockInfluxDBStorage


@pytest.fixture(autouse=True)
def setup_mocks():
    """Setup all mocks."""
    register_mock_protocols()
    register_mock_storage()
    yield


@pytest.mark.django_db
class TestEndToEndAcquisition:
    """Test complete acquisition pipeline."""

    @patch("acquisition.services.acquisition_service.settings")
    @patch("acquisition.services.acquisition_service.StorageRegistry")
    def test_full_acquisition_pipeline(
        self,
        mock_storage_registry,
        mock_settings,
        create_site,
        create_device,
        create_point,
        create_task,
        create_session,
    ):
        """Test complete acquisition from device to storage."""
        # Setup settings
        mock_settings.INFLUXDB_HOST = "localhost"
        mock_settings.INFLUXDB_PORT = 8086
        mock_settings.INFLUXDB_TOKEN = "test-token"
        mock_settings.INFLUXDB_ORG = "test-org"
        mock_settings.INFLUXDB_BUCKET = "test-bucket"
        mock_settings.KAFKA_ENABLED = False

        # Create real mock storage to capture data
        mock_storage = MockInfluxDBStorage({})
        mock_storage_registry.create.return_value = mock_storage

        # Setup test data
        site = create_site(code="FACTORY_01", name="Test Factory")
        device = create_device(
            site=site,
            protocol="mock_modbus",
            ip="192.168.1.100",
            port=502,
            code="MODBUS_001",
            metadata={
                "device_a_tag": "SENSOR_RACK_01",
                "_test_simulated_data": {
                    "TEMP_01": 25.5,
                    "PRESSURE_01": 101.3,
                    "FLOW_01": 150.0,
                },
            },
        )

        # Create points
        temp_point = create_point(
            device=device,
            code="TEMP_01",
            address="D100",
            extra={"type": 3, "num": 1},
        )
        pressure_point = create_point(
            device=device,
            code="PRESSURE_01",
            address="D101",
            extra={"type": 3, "num": 1},
        )
        flow_point = create_point(
            device=device,
            code="FLOW_01",
            address="D102",
            extra={"type": 3, "num": 1},
        )

        # Create task
        task = create_task(
            code="SENSOR_MONITORING",
            name="Sensor Rack Monitoring",
            points=[temp_point, pressure_point, flow_point],
        )

        # Create session
        session = create_session(task=task)

        # Execute acquisition
        service = AcquisitionService(task, session)
        result = service.acquire_once()

        # Verify result
        assert result["status"] == "completed"
        assert result["points_read"] == 3
        assert len(result["errors"]) == 0

        # Verify data was written to storage
        written_data = mock_storage.get_written_data()
        assert len(written_data) == 3

        # Verify data structure
        temp_data = next((d for d in written_data if d["fields"].get("TEMP_01")), None)
        assert temp_data is not None
        assert temp_data["measurement"] == "SENSOR_RACK_01"
        assert temp_data["tags"]["site"] == "FACTORY_01"
        assert temp_data["tags"]["device"] == "MODBUS_001"
        assert temp_data["fields"]["TEMP_01"] == 25.5

    @patch("acquisition.services.acquisition_service.settings")
    @patch("acquisition.services.acquisition_service.StorageRegistry")
    def test_multi_device_acquisition(
        self,
        mock_storage_registry,
        mock_settings,
        create_site,
        create_device,
        create_point,
        create_task,
        create_session,
    ):
        """Test acquisition from multiple devices."""
        mock_settings.INFLUXDB_HOST = "localhost"
        mock_settings.KAFKA_ENABLED = False

        mock_storage = MockInfluxDBStorage({})
        mock_storage_registry.create.return_value = mock_storage

        site = create_site()

        # Create 2 devices with different protocols
        modbus_device = create_device(
            site=site,
            protocol="mock_modbus",
            code="MODBUS_001",
            metadata={"_test_simulated_data": {"MB_P1": 100}},
        )
        plc_device = create_device(
            site=site,
            protocol="mock_plc",
            code="PLC_001",
            metadata={"_test_simulated_data": {"PLC_P1": 200}},
        )

        # Create points for each device
        mb_point = create_point(device=modbus_device, code="MB_P1")
        plc_point = create_point(device=plc_device, code="PLC_P1")

        # Create task with points from both devices
        task = create_task(points=[mb_point, plc_point])
        session = create_session(task=task)

        # Execute
        service = AcquisitionService(task, session)
        result = service.acquire_once()

        # Should read from both devices
        assert result["status"] == "completed"
        assert result["points_read"] == 2

        # Verify both devices' data
        written_data = mock_storage.get_written_data()
        codes = [list(d["fields"].keys())[0] for d in written_data]
        assert "MB_P1" in codes
        assert "PLC_P1" in codes

    @patch("acquisition.services.acquisition_service.settings")
    @patch("acquisition.services.acquisition_service.StorageRegistry")
    def test_partial_failure_handling(
        self,
        mock_storage_registry,
        mock_settings,
        create_device,
        create_point,
        create_task,
        create_session,
    ):
        """Test handling when one device fails."""
        mock_settings.INFLUXDB_HOST = "localhost"
        mock_settings.KAFKA_ENABLED = False

        mock_storage = MockInfluxDBStorage({})
        mock_storage_registry.create.return_value = mock_storage

        # One working device
        good_device = create_device(
            protocol="mock_modbus",
            code="GOOD_DEV",
            metadata={"_test_simulated_data": {"P1": 100}},
        )

        # One failing device
        bad_device = create_device(
            protocol="mock_modbus",
            code="BAD_DEV",
            metadata={"_test_connection_fail": True},
        )

        good_point = create_point(device=good_device, code="P1")
        bad_point = create_point(device=bad_device, code="P2")

        task = create_task(points=[good_point, bad_point])
        session = create_session(task=task)

        # Execute
        service = AcquisitionService(task, session)
        result = service.acquire_once()

        # Should complete but with errors
        assert result["status"] == "completed"
        assert result["points_read"] == 1  # Only good device
        assert len(result["errors"]) == 1  # Bad device error

        # Only good device's data should be stored
        written_data = mock_storage.get_written_data()
        assert len(written_data) == 1
        assert "P1" in list(written_data[0]["fields"].keys())

    @patch("acquisition.services.acquisition_service.settings")
    @patch("acquisition.services.acquisition_service.StorageRegistry")
    def test_storage_failure_handling(
        self,
        mock_storage_registry,
        mock_settings,
        create_device,
        create_point,
        create_task,
        create_session,
    ):
        """Test handling when storage write fails."""
        mock_settings.INFLUXDB_HOST = "localhost"
        mock_settings.KAFKA_ENABLED = False

        # Storage that will fail
        mock_storage = MockInfluxDBStorage({"_test_write_fail": True})
        mock_storage_registry.create.return_value = mock_storage

        device = create_device(
            protocol="mock_modbus",
            metadata={"_test_simulated_data": {"P1": 100}},
        )
        point = create_point(device=device, code="P1")

        task = create_task(points=[point])
        session = create_session(task=task)

        # Execute
        service = AcquisitionService(task, session)

        # Should not raise exception, just log error
        result = service.acquire_once()

        # Data was read successfully
        assert result["points_read"] == 1
        # But storage write failed (logged, not in errors)


@pytest.mark.django_db
class TestProtocolInteroperability:
    """Test different protocols working together."""

    @patch("acquisition.services.acquisition_service.settings")
    @patch("acquisition.services.acquisition_service.StorageRegistry")
    def test_mixed_protocol_acquisition(
        self,
        mock_storage_registry,
        mock_settings,
        create_device,
        create_point,
        create_task,
        create_session,
    ):
        """Test acquisition with ModbusTCP, PLC, and MQTT together."""
        mock_settings.INFLUXDB_HOST = "localhost"
        mock_settings.KAFKA_ENABLED = False

        mock_storage = MockInfluxDBStorage({})
        mock_storage_registry.create.return_value = mock_storage

        # Create devices with different protocols
        modbus_dev = create_device(
            protocol="mock_modbus",
            code="MB",
            metadata={"_test_simulated_data": {"MB_TEMP": 25.0}},
        )
        plc_dev = create_device(
            protocol="mock_plc",
            code="PLC",
            metadata={"_test_simulated_data": {"PLC_PRESSURE": 100.0}},
        )
        mqtt_dev = create_device(
            protocol="mock_mqtt",
            code="MQTT",
            metadata={
                "_test_messages": [
                    {
                        "code": "MQTT_SENSOR",
                        "value": {"humidity": 60.0},
                        "timestamp": 1234567890000000000,
                        "quality": "good",
                    }
                ]
            },
        )

        # Create points
        mb_point = create_point(device=modbus_dev, code="MB_TEMP")
        plc_point = create_point(device=plc_dev, code="PLC_PRESSURE")
        mqtt_point = create_point(device=mqtt_dev, code="MQTT_SENSOR")

        task = create_task(points=[mb_point, plc_point, mqtt_point])
        session = create_session(task=task)

        # Execute
        service = AcquisitionService(task, session)
        result = service.acquire_once()

        # All protocols should work
        assert result["status"] == "completed"
        assert result["points_read"] == 3
        assert len(result["errors"]) == 0

        # Verify data from all protocols
        written_data = mock_storage.get_written_data()
        assert len(written_data) == 3


@pytest.mark.django_db
class TestDataFormatting:
    """Test data formatting for storage."""

    @patch("acquisition.services.acquisition_service.settings")
    @patch("acquisition.services.acquisition_service.StorageRegistry")
    def test_point_template_applied(
        self,
        mock_storage_registry,
        mock_settings,
        create_device,
        create_point,
        create_point_template,
        create_task,
        create_session,
    ):
        """Test that point template metadata is included."""
        mock_settings.INFLUXDB_HOST = "localhost"
        mock_settings.KAFKA_ENABLED = False

        mock_storage = MockInfluxDBStorage({})
        mock_storage_registry.create.return_value = mock_storage

        # Create point with template
        template = create_point_template(
            name="温度",
            unit="°C",
            data_type="float",
            coefficient="0.1",
            precision=2,
        )

        device = create_device(
            protocol="mock_modbus",
            metadata={"_test_simulated_data": {"TEMP": 250}},  # Will be *0.1 = 25.0
        )

        point = create_point(device=device, code="TEMP", template=template)

        task = create_task(points=[point])
        session = create_session(task=task)

        # Execute
        service = AcquisitionService(task, session)
        service.acquire_once()

        # Check formatted data
        written_data = mock_storage.get_written_data()
        assert len(written_data) == 1

        data = written_data[0]
        assert data["tags"]["cn_name"] == "温度"
        assert data["tags"]["unit"] == "°C"
