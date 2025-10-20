"""Mock storage implementations for testing."""
from __future__ import annotations

from typing import Any, Dict, List

from storage.base import BaseStorage, StorageRegistry


class MockInfluxDBStorage(BaseStorage):
    """
    Mock InfluxDB storage for testing.

    Stores data in memory instead of writing to real InfluxDB.
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.written_data: List[Dict[str, Any]] = []
        self.write_should_fail = config.get("_test_write_fail", False)
        self.connect_should_fail = config.get("_test_connect_fail", False)

    def connect(self) -> bool:
        """Simulate connection."""
        if self.connect_should_fail:
            self.is_connected = False
            return False

        self.is_connected = True
        self.logger.info("Mock InfluxDB: Connected")
        return True

    def disconnect(self) -> None:
        """Simulate disconnection."""
        self.is_connected = False
        self.logger.info("Mock InfluxDB: Disconnected")

    def write(self, data: List[Dict[str, Any]]) -> bool:
        """
        Mock write operation.

        Stores data in memory for later verification.
        """
        if self.write_should_fail:
            raise Exception("Simulated write failure")

        if not self.is_connected:
            raise Exception("Not connected to InfluxDB")

        # Store data for test verification
        self.written_data.extend(data)

        self.logger.info(f"Mock InfluxDB: Wrote {len(data)} points")
        return True

    def health_check(self) -> bool:
        """Simulate health check."""
        return self.is_connected

    def get_written_data(self) -> List[Dict[str, Any]]:
        """Get all data written during tests."""
        return self.written_data

    def clear_data(self) -> None:
        """Clear stored data."""
        self.written_data = []


class MockKafkaStorage(BaseStorage):
    """
    Mock Kafka storage for testing.

    Stores messages in memory instead of sending to Kafka.
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.sent_messages: List[Dict[str, Any]] = []
        self.write_should_fail = config.get("_test_write_fail", False)

    def connect(self) -> bool:
        """Simulate connection."""
        self.is_connected = True
        self.logger.info("Mock Kafka: Connected")
        return True

    def disconnect(self) -> None:
        """Simulate disconnection."""
        self.is_connected = False

    def write(self, data: List[Dict[str, Any]]) -> bool:
        """
        Mock message sending.

        Stores messages in memory for verification.
        """
        if self.write_should_fail:
            raise Exception("Simulated Kafka send failure")

        if not self.is_connected:
            raise Exception("Not connected to Kafka")

        # Store messages
        self.sent_messages.extend(data)

        self.logger.info(f"Mock Kafka: Sent {len(data)} messages")
        return True

    def health_check(self) -> bool:
        """Simulate health check."""
        return self.is_connected

    def get_sent_messages(self) -> List[Dict[str, Any]]:
        """Get all messages sent during tests."""
        return self.sent_messages

    def clear_messages(self) -> None:
        """Clear stored messages."""
        self.sent_messages = []


# Register mock storage backends
def register_mock_storage():
    """Register all mock storage backends."""
    StorageRegistry.register("mock_influxdb")(MockInfluxDBStorage)
    StorageRegistry.register("mock_kafka")(MockKafkaStorage)
