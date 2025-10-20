"""Base protocol interface and registry for all acquisition protocols."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Type

logger = logging.getLogger(__name__)


class ProtocolError(Exception):
    """Base exception for protocol-related errors."""
    pass


class ConnectionError(ProtocolError):
    """Raised when connection to device fails."""
    pass


class ReadError(ProtocolError):
    """Raised when reading data fails."""
    pass


class BaseProtocol(ABC):
    """
    Abstract base class for all protocol implementations.

    All protocol adapters must implement this interface to ensure
    consistent behavior across different industrial communication protocols.
    """

    def __init__(self, device_config: Dict[str, Any]) -> None:
        """
        Initialize protocol with device configuration.

        Args:
            device_config: Dictionary containing connection parameters like:
                - source_ip: str
                - source_port: int
                - protocol_type: str
                - Additional protocol-specific parameters
        """
        self.device_config = device_config
        self.is_connected = False
        self.logger = logging.getLogger(f"{self.__class__.__module__}.{self.__class__.__name__}")

    @abstractmethod
    def connect(self) -> bool:
        """
        Establish connection to the device.

        Returns:
            True if connection successful, False otherwise.

        Raises:
            ConnectionError: If connection fails critically.
        """
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """
        Close connection to the device gracefully.
        """
        pass

    @abstractmethod
    def read_points(self, points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Read data from specified points.

        Args:
            points: List of point configurations, each containing:
                - code: str (point identifier)
                - address: str (protocol-specific address)
                - Additional point-specific parameters

        Returns:
            List of readings, each containing:
                - code: str
                - value: Any
                - timestamp: float (Unix timestamp in nanoseconds)
                - quality: str ('good', 'bad', 'uncertain')
                - Additional metadata

        Raises:
            ReadError: If read operation fails.
        """
        pass

    @abstractmethod
    def health_check(self) -> bool:
        """
        Check if connection is still alive and healthy.

        Returns:
            True if healthy, False otherwise.
        """
        pass

    def __enter__(self):
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.disconnect()


class ProtocolRegistry:
    """
    Registry for managing protocol implementations.

    Uses factory pattern to instantiate protocols by name.
    """

    _protocols: Dict[str, Type[BaseProtocol]] = {}

    @classmethod
    def register(cls, protocol_name: str) -> callable:
        """
        Decorator to register a protocol implementation.

        Usage:
            @ProtocolRegistry.register('modbus')
            class ModbusProtocol(BaseProtocol):
                ...
        """
        def decorator(protocol_class: Type[BaseProtocol]) -> Type[BaseProtocol]:
            if not issubclass(protocol_class, BaseProtocol):
                raise TypeError(f"{protocol_class} must inherit from BaseProtocol")
            cls._protocols[protocol_name.lower()] = protocol_class
            logger.info(f"Registered protocol: {protocol_name} -> {protocol_class.__name__}")
            return protocol_class
        return decorator

    @classmethod
    def create(cls, protocol_name: str, device_config: Dict[str, Any]) -> BaseProtocol:
        """
        Factory method to create protocol instance.

        Args:
            protocol_name: Name of the protocol (e.g., 'modbus', 'plc')
            device_config: Configuration dictionary for the device

        Returns:
            Instance of the requested protocol.

        Raises:
            ValueError: If protocol is not registered.
        """
        protocol_name = protocol_name.lower()
        if protocol_name not in cls._protocols:
            raise ValueError(
                f"Protocol '{protocol_name}' not registered. "
                f"Available: {list(cls._protocols.keys())}"
            )
        return cls._protocols[protocol_name](device_config)

    @classmethod
    def list_protocols(cls) -> List[str]:
        """Return list of registered protocol names."""
        return list(cls._protocols.keys())

    @classmethod
    def clear(cls) -> None:
        """Clear all registered protocols (mainly for testing)."""
        cls._protocols.clear()
