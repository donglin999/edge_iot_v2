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

# 模拟设备:import 后并不自动注册,只有 EDGE_ENABLE_SIMULATOR=1 时才进注册表。
# 生产环境的协议下拉里不该出现一个假协议。
from . import simulator  # noqa: F401   simulator（默认关闭）

__all__ = [
    "BaseProtocol",
    "ConnectionError",
    "FieldSpec",
    "ProtocolError",
    "ProtocolMeta",
    "ProtocolRegistry",
    "ReadError",
]
