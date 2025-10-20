"""Mock protocol implementations for testing."""
from __future__ import annotations

import time
from typing import Any, Dict, List
from unittest.mock import MagicMock

from acquisition.protocols.base import BaseProtocol, ProtocolRegistry


class MockModbusTCPProtocol(BaseProtocol):
    """
    Mock ModbusTCP protocol for testing.

    Simulates device responses without actual hardware.
    """

    def __init__(self, device_config: Dict[str, Any]) -> None:
        super().__init__(device_config)
        self.connection_should_fail = device_config.get("_test_connection_fail", False)
        self.read_should_fail = device_config.get("_test_read_fail", False)
        self.simulated_data = device_config.get("_test_simulated_data", {})

    def connect(self) -> bool:
        """Simulate connection."""
        if self.connection_should_fail:
            self.is_connected = False
            return False

        self.logger.info(f"Mock: Connected to {self.device_config.get('source_ip')}")
        self.is_connected = True
        return True

    def disconnect(self) -> None:
        """Simulate disconnection."""
        self.is_connected = False
        self.logger.info("Mock: Disconnected")

    def read_points(self, points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Simulate reading points.

        Returns predefined test data or random values.
        """
        if self.read_should_fail:
            raise Exception("Simulated read failure")

        results = []
        for point in points:
            point_code = point["code"]

            # Use simulated data if provided
            if point_code in self.simulated_data:
                value = self.simulated_data[point_code]
            else:
                # Generate mock data based on type
                data_type = point.get("type", 3)
                if data_type in [3, 4]:  # Holding/Input registers
                    value = 100 + len(point_code)  # Deterministic mock value
                else:
                    value = 1

            results.append({
                "code": point_code,
                "value": value,
                "timestamp": time.time_ns(),
                "quality": "good",
                "address": point.get("address", 0),
            })

        return results

    def health_check(self) -> bool:
        """Simulate health check."""
        return self.is_connected


class MockPLCProtocol(BaseProtocol):
    """Mock Mitsubishi PLC protocol for testing."""

    def __init__(self, device_config: Dict[str, Any]) -> None:
        super().__init__(device_config)
        self.connection_should_fail = device_config.get("_test_connection_fail", False)
        self.simulated_data = device_config.get("_test_simulated_data", {})

    def connect(self) -> bool:
        """Simulate PLC connection."""
        if self.connection_should_fail:
            self.is_connected = False
            return False

        self.is_connected = True
        self.logger.info("Mock PLC: Connected")
        return True

    def disconnect(self) -> None:
        """Simulate disconnection."""
        self.is_connected = False

    def read_points(self, points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Simulate PLC data reading."""
        results = []
        for point in points:
            point_code = point["code"]
            data_type = point.get("type", "int16")

            # Generate type-specific mock data
            if point_code in self.simulated_data:
                value = self.simulated_data[point_code]
            elif data_type == "int16":
                value = 42
            elif data_type == "int32":
                value = 12345
            elif data_type == "float":
                value = 25.5
            elif data_type == "bool":
                value = 1
            elif data_type == "str":
                value = "TEST_STRING"
            else:
                value = 0

            results.append({
                "code": point_code,
                "value": value,
                "timestamp": time.time_ns(),
                "quality": "good",
            })

        return results

    def health_check(self) -> bool:
        """Simulate health check."""
        return self.is_connected


class MockMQTTProtocol(BaseProtocol):
    """Mock MQTT protocol for testing."""

    def __init__(self, device_config: Dict[str, Any]) -> None:
        super().__init__(device_config)
        self.topics = device_config.get("mqtt_topics", ["test/topic"])
        self.simulated_messages = device_config.get("_test_messages", [])
        self.message_index = 0

    def connect(self) -> bool:
        """Simulate MQTT connection."""
        self.is_connected = True
        self.logger.info(f"Mock MQTT: Connected, subscribed to {self.topics}")
        return True

    def disconnect(self) -> None:
        """Simulate disconnection."""
        self.is_connected = False

    def read_points(self, points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Simulate receiving MQTT messages.

        Returns messages from simulated_messages queue.
        """
        if not self.simulated_messages:
            # Generate default message
            return [{
                "code": points[0]["code"] if points else "default",
                "value": {"temperature": 25.0, "humidity": 60.0},
                "timestamp": time.time_ns(),
                "quality": "good",
                "topic": self.topics[0],
            }]

        # Return next simulated message
        if self.message_index < len(self.simulated_messages):
            msg = self.simulated_messages[self.message_index]
            self.message_index += 1
            return [msg]

        return []

    def health_check(self) -> bool:
        """Simulate health check."""
        return self.is_connected


# Register mock protocols for testing
def register_mock_protocols():
    """Register all mock protocols."""
    ProtocolRegistry.register("mock_modbus")(MockModbusTCPProtocol)
    ProtocolRegistry.register("mock_plc")(MockPLCProtocol)
    ProtocolRegistry.register("mock_mqtt")(MockMQTTProtocol)
