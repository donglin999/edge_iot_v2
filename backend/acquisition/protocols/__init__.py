"""Protocol adapters for various industrial communication protocols.

Adding a new protocol → see ``protocols/README.md``.
"""
from .base import (
    BaseProtocol,
    ConnectionError,
    FieldSpec,
    ProtocolError,
    ProtocolMeta,
    ProtocolRegistry,
    ReadError,
)

# Importing each module triggers @ProtocolRegistry.register on its classes.
# Order doesn't matter, but keep it stable for predictable logs.
from . import modbus  # noqa: F401   modbus_tcp + modbus_rtu
from . import mqtt    # noqa: F401   mqtt
from . import scada   # noqa: F401   scada (mqtt-based SCADA gateway)
from . import opcua   # noqa: F401   opcua
from . import s7      # noqa: F401   siemens_s7

__all__ = [
    "BaseProtocol",
    "ConnectionError",
    "FieldSpec",
    "ProtocolError",
    "ProtocolMeta",
    "ProtocolRegistry",
    "ReadError",
]
