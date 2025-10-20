"""Common exceptions for the platform."""
from __future__ import annotations


class PlatformError(Exception):
    """Base exception for platform-wide errors."""
    pass


class ConfigurationError(PlatformError):
    """Raised when configuration is invalid or missing."""
    pass


class AcquisitionError(PlatformError):
    """Raised when data acquisition fails."""
    pass


class StorageError(PlatformError):
    """Raised when storage operations fail."""
    pass


class ProtocolError(PlatformError):
    """Raised when protocol communication fails."""
    pass


class ValidationError(PlatformError):
    """Raised when data validation fails."""
    pass
