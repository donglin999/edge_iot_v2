"""Base storage interface for data persistence."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Type

logger = logging.getLogger(__name__)


class StorageError(Exception):
    """Base exception for storage-related errors."""
    pass


class WriteError(StorageError):
    """Raised when write operation fails."""
    pass


class BaseStorage(ABC):
    """
    Abstract base class for all storage backends.

    Provides unified interface for writing acquisition data
    to various persistence systems (InfluxDB, Kafka, etc.).
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        """
        Initialize storage with configuration.

        Args:
            config: Storage-specific configuration parameters
        """
        self.config = config
        self.is_connected = False
        self.logger = logging.getLogger(f"{self.__class__.__module__}.{self.__class__.__name__}")

    @abstractmethod
    def connect(self) -> bool:
        """
        Establish connection to storage backend.

        Returns:
            True if connection successful.

        Raises:
            StorageError: If connection fails.
        """
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """Close connection to storage backend."""
        pass

    @abstractmethod
    def write(self, data: List[Dict[str, Any]]) -> bool:
        """
        Write data points to storage.

        Args:
            data: List of data points, each containing:
                - measurement: str (metric name)
                - tags: Dict[str, str] (metadata)
                - fields: Dict[str, Any] (actual values)
                - timestamp: Optional[int] (nanoseconds)

        Returns:
            True if write successful.

        Raises:
            WriteError: If write operation fails.
        """
        pass

    @abstractmethod
    def health_check(self) -> bool:
        """
        Check if storage backend is healthy.

        Returns:
            True if healthy.
        """
        pass

    def __enter__(self):
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.disconnect()


class StorageRegistry:
    """Registry for managing storage implementations."""

    _storages: Dict[str, Type[BaseStorage]] = {}

    @classmethod
    def register(cls, storage_name: str) -> callable:
        """
        Decorator to register a storage implementation.

        Usage:
            @StorageRegistry.register('influxdb')
            class InfluxDBStorage(BaseStorage):
                ...
        """
        def decorator(storage_class: Type[BaseStorage]) -> Type[BaseStorage]:
            if not issubclass(storage_class, BaseStorage):
                raise TypeError(f"{storage_class} must inherit from BaseStorage")
            cls._storages[storage_name.lower()] = storage_class
            logger.info(f"Registered storage: {storage_name} -> {storage_class.__name__}")
            return storage_class
        return decorator

    @classmethod
    def create(cls, storage_name: str, config: Dict[str, Any]) -> BaseStorage:
        """
        Factory method to create storage instance.

        Args:
            storage_name: Name of the storage (e.g., 'influxdb', 'kafka')
            config: Configuration dictionary for the storage

        Returns:
            Instance of the requested storage.

        Raises:
            ValueError: If storage is not registered.
        """
        storage_name = storage_name.lower()
        if storage_name not in cls._storages:
            raise ValueError(
                f"Storage '{storage_name}' not registered. "
                f"Available: {list(cls._storages.keys())}"
            )
        return cls._storages[storage_name](config)

    @classmethod
    def list_storages(cls) -> List[str]:
        """Return list of registered storage names."""
        return list(cls._storages.keys())

    @classmethod
    def clear(cls) -> None:
        """Clear all registered storages (mainly for testing)."""
        cls._storages.clear()
