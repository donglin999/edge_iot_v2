"""Protocol adapters for various industrial communication protocols."""
from .base import BaseProtocol, ProtocolRegistry

# Import all protocol implementations to trigger registration
from . import modbus  # noqa: F401
from . import mqtt  # noqa: F401

__all__ = ["BaseProtocol", "ProtocolRegistry"]
