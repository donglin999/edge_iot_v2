"""Unit tests for protocol layer."""
import pytest

from acquisition.protocols import ProtocolRegistry
from tests.mocks.protocols import register_mock_protocols


@pytest.fixture(autouse=True)
def setup_mock_protocols():
    """Register mock protocols before each test."""
    register_mock_protocols()
    yield
    # No need to clear, registry persists


class TestProtocolRegistry:
    """Test protocol registry functionality."""

    def test_register_protocol(self):
        """Test protocol registration."""
        assert "mock_modbus" in ProtocolRegistry.list_protocols()
        assert "mock_plc" in ProtocolRegistry.list_protocols()
        assert "mock_mqtt" in ProtocolRegistry.list_protocols()

    def test_create_protocol(self, sample_device_config):
        """Test protocol factory creation."""
        protocol = ProtocolRegistry.create("mock_modbus", sample_device_config)
        assert protocol is not None
        assert not protocol.is_connected

    def test_create_invalid_protocol(self):
        """Test creation of unregistered protocol."""
        with pytest.raises(ValueError, match="not registered"):
            ProtocolRegistry.create("invalid_protocol", {})


class TestMockModbusTCP:
    """Test MockModbusTCP protocol."""

    def test_connection_success(self, sample_device_config):
        """Test successful connection."""
        protocol = ProtocolRegistry.create("mock_modbus", sample_device_config)

        assert protocol.connect()
        assert protocol.is_connected
        assert protocol.health_check()

    def test_connection_failure(self, sample_device_config):
        """Test connection failure simulation."""
        sample_device_config["_test_connection_fail"] = True
        protocol = ProtocolRegistry.create("mock_modbus", sample_device_config)

        assert not protocol.connect()
        assert not protocol.is_connected
        assert not protocol.health_check()

    def test_read_points_success(self, sample_device_config, sample_points_config):
        """Test successful point reading."""
        protocol = ProtocolRegistry.create("mock_modbus", sample_device_config)
        protocol.connect()

        results = protocol.read_points(sample_points_config)

        assert len(results) == 3
        assert results[0]["code"] == "POINT_001"
        assert results[0]["value"] == 100  # From simulated_data
        assert results[0]["quality"] == "good"
        assert "timestamp" in results[0]

    def test_read_points_without_connection(self, sample_device_config, sample_points_config):
        """Test reading points without connection."""
        protocol = ProtocolRegistry.create("mock_modbus", sample_device_config)
        # Don't connect

        # Mock should still work (doesn't require actual connection)
        results = protocol.read_points(sample_points_config)
        assert len(results) > 0

    def test_read_failure(self, sample_device_config, sample_points_config):
        """Test read operation failure."""
        sample_device_config["_test_read_fail"] = True
        protocol = ProtocolRegistry.create("mock_modbus", sample_device_config)
        protocol.connect()

        with pytest.raises(Exception, match="Simulated read failure"):
            protocol.read_points(sample_points_config)

    def test_disconnect(self, sample_device_config):
        """Test disconnection."""
        protocol = ProtocolRegistry.create("mock_modbus", sample_device_config)
        protocol.connect()

        assert protocol.is_connected

        protocol.disconnect()
        assert not protocol.is_connected

    def test_context_manager(self, sample_device_config, sample_points_config):
        """Test protocol as context manager."""
        protocol = ProtocolRegistry.create("mock_modbus", sample_device_config)

        with protocol:
            assert protocol.is_connected
            results = protocol.read_points(sample_points_config)
            assert len(results) == 3

        assert not protocol.is_connected


class TestMockPLC:
    """Test MockPLC protocol."""

    def test_plc_connection(self):
        """Test PLC connection."""
        config = {"source_ip": "192.168.1.10", "source_port": 6000}
        protocol = ProtocolRegistry.create("mock_plc", config)

        assert protocol.connect()
        assert protocol.is_connected

    def test_plc_read_different_types(self):
        """Test reading different data types from PLC."""
        config = {
            "_test_simulated_data": {
                "INT_POINT": 42,
                "FLOAT_POINT": 25.5,
                "BOOL_POINT": 1,
                "STR_POINT": "TEST",
            }
        }
        protocol = ProtocolRegistry.create("mock_plc", config)
        protocol.connect()

        points = [
            {"code": "INT_POINT", "type": "int16"},
            {"code": "FLOAT_POINT", "type": "float"},
            {"code": "BOOL_POINT", "type": "bool"},
            {"code": "STR_POINT", "type": "str"},
        ]

        results = protocol.read_points(points)

        assert len(results) == 4
        assert results[0]["value"] == 42
        assert results[1]["value"] == 25.5
        assert results[2]["value"] == 1
        assert results[3]["value"] == "TEST"


class TestMockMQTT:
    """Test MockMQTT protocol."""

    def test_mqtt_connection(self):
        """Test MQTT connection."""
        config = {
            "source_ip": "mqtt.example.com",
            "source_port": 1883,
            "mqtt_topics": ["test/topic"],
        }
        protocol = ProtocolRegistry.create("mock_mqtt", config)

        assert protocol.connect()
        assert protocol.is_connected

    def test_mqtt_read_messages(self):
        """Test reading MQTT messages."""
        config = {
            "mqtt_topics": ["sensor/data"],
            "_test_messages": [
                {
                    "code": "SENSOR_001",
                    "value": {"temperature": 25.0, "humidity": 60.0},
                    "timestamp": 1234567890000000000,
                    "quality": "good",
                    "topic": "sensor/data",
                }
            ],
        }
        protocol = ProtocolRegistry.create("mock_mqtt", config)
        protocol.connect()

        points = [{"code": "SENSOR_001"}]
        results = protocol.read_points(points)

        assert len(results) == 1
        assert results[0]["code"] == "SENSOR_001"
        assert results[0]["value"]["temperature"] == 25.0
        assert results[0]["topic"] == "sensor/data"

    def test_mqtt_multiple_messages(self):
        """Test reading multiple MQTT messages."""
        config = {
            "_test_messages": [
                {"code": "MSG_1", "value": 1, "timestamp": 1000, "quality": "good"},
                {"code": "MSG_2", "value": 2, "timestamp": 2000, "quality": "good"},
            ],
        }
        protocol = ProtocolRegistry.create("mock_mqtt", config)
        protocol.connect()

        points = [{"code": "MSG"}]

        # First read
        results1 = protocol.read_points(points)
        assert results1[0]["code"] == "MSG_1"

        # Second read
        results2 = protocol.read_points(points)
        assert results2[0]["code"] == "MSG_2"

        # Third read (no more messages)
        results3 = protocol.read_points(points)
        assert len(results3) == 0
