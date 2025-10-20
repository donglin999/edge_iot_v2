"""Unit tests for storage layer."""
import pytest

from storage import StorageRegistry
from tests.mocks.storage import register_mock_storage


@pytest.fixture(autouse=True)
def setup_mock_storage():
    """Register mock storage before each test."""
    register_mock_storage()
    yield


class TestStorageRegistry:
    """Test storage registry functionality."""

    def test_register_storage(self):
        """Test storage registration."""
        assert "mock_influxdb" in StorageRegistry.list_storages()
        assert "mock_kafka" in StorageRegistry.list_storages()

    def test_create_storage(self, sample_storage_config):
        """Test storage factory creation."""
        storage = StorageRegistry.create("mock_influxdb", sample_storage_config)
        assert storage is not None
        assert not storage.is_connected

    def test_create_invalid_storage(self):
        """Test creation of unregistered storage."""
        with pytest.raises(ValueError, match="not registered"):
            StorageRegistry.create("invalid_storage", {})


class TestMockInfluxDB:
    """Test MockInfluxDB storage."""

    def test_connection_success(self, sample_storage_config):
        """Test successful connection."""
        storage = StorageRegistry.create("mock_influxdb", sample_storage_config)

        assert storage.connect()
        assert storage.is_connected
        assert storage.health_check()

    def test_connection_failure(self, sample_storage_config):
        """Test connection failure simulation."""
        sample_storage_config["_test_connect_fail"] = True
        storage = StorageRegistry.create("mock_influxdb", sample_storage_config)

        assert not storage.connect()
        assert not storage.is_connected

    def test_write_data_success(self, sample_storage_config):
        """Test successful data writing."""
        storage = StorageRegistry.create("mock_influxdb", sample_storage_config)
        storage.connect()

        data = [
            {
                "measurement": "test_measurement",
                "tags": {"site": "test_site", "device": "device_001"},
                "fields": {"temperature": 25.5, "humidity": 60.0},
                "time": 1234567890000000000,
            },
            {
                "measurement": "test_measurement",
                "tags": {"site": "test_site", "device": "device_002"},
                "fields": {"temperature": 26.0},
                "time": 1234567891000000000,
            },
        ]

        assert storage.write(data)

        # Verify data was stored
        written = storage.get_written_data()
        assert len(written) == 2
        assert written[0]["measurement"] == "test_measurement"
        assert written[0]["fields"]["temperature"] == 25.5

    def test_write_without_connection(self, sample_storage_config):
        """Test writing without connection."""
        storage = StorageRegistry.create("mock_influxdb", sample_storage_config)
        # Don't connect

        data = [{"measurement": "test", "fields": {"value": 1}}]

        with pytest.raises(Exception, match="Not connected"):
            storage.write(data)

    def test_write_failure(self, sample_storage_config):
        """Test write operation failure."""
        sample_storage_config["_test_write_fail"] = True
        storage = StorageRegistry.create("mock_influxdb", sample_storage_config)
        storage.connect()

        data = [{"measurement": "test", "fields": {"value": 1}}]

        with pytest.raises(Exception, match="Simulated write failure"):
            storage.write(data)

    def test_empty_write(self, sample_storage_config):
        """Test writing empty data."""
        storage = StorageRegistry.create("mock_influxdb", sample_storage_config)
        storage.connect()

        assert storage.write([])

        written = storage.get_written_data()
        assert len(written) == 0

    def test_disconnect(self, sample_storage_config):
        """Test disconnection."""
        storage = StorageRegistry.create("mock_influxdb", sample_storage_config)
        storage.connect()

        assert storage.is_connected

        storage.disconnect()
        assert not storage.is_connected

    def test_context_manager(self, sample_storage_config):
        """Test storage as context manager."""
        storage = StorageRegistry.create("mock_influxdb", sample_storage_config)

        with storage:
            assert storage.is_connected
            data = [{"measurement": "test", "fields": {"value": 42}}]
            storage.write(data)

        assert not storage.is_connected

    def test_clear_data(self, sample_storage_config):
        """Test clearing stored data."""
        storage = StorageRegistry.create("mock_influxdb", sample_storage_config)
        storage.connect()

        # Write some data
        storage.write([{"measurement": "test", "fields": {"value": 1}}])
        assert len(storage.get_written_data()) == 1

        # Clear data
        storage.clear_data()
        assert len(storage.get_written_data()) == 0


class TestMockKafka:
    """Test MockKafka storage."""

    def test_kafka_connection(self):
        """Test Kafka connection."""
        config = {"bootstrap_servers": "localhost:9092", "topic": "test_topic"}
        storage = StorageRegistry.create("mock_kafka", config)

        assert storage.connect()
        assert storage.is_connected

    def test_kafka_send_messages(self):
        """Test sending Kafka messages."""
        config = {"bootstrap_servers": "localhost:9092", "topic": "sensor_data"}
        storage = StorageRegistry.create("mock_kafka", config)
        storage.connect()

        messages = [
            {"topic": "sensor_data", "key": "device_001", "value": {"temp": 25.0}},
            {"topic": "sensor_data", "key": "device_002", "value": {"temp": 26.0}},
        ]

        assert storage.write(messages)

        # Verify messages were sent
        sent = storage.get_sent_messages()
        assert len(sent) == 2
        assert sent[0]["value"]["temp"] == 25.0

    def test_kafka_write_failure(self):
        """Test Kafka send failure."""
        config = {"_test_write_fail": True}
        storage = StorageRegistry.create("mock_kafka", config)
        storage.connect()

        messages = [{"value": {"test": 1}}]

        with pytest.raises(Exception, match="Simulated Kafka send failure"):
            storage.write(messages)

    def test_kafka_clear_messages(self):
        """Test clearing sent messages."""
        config = {}
        storage = StorageRegistry.create("mock_kafka", config)
        storage.connect()

        # Send messages
        storage.write([{"value": {"test": 1}}])
        assert len(storage.get_sent_messages()) == 1

        # Clear messages
        storage.clear_messages()
        assert len(storage.get_sent_messages()) == 0
