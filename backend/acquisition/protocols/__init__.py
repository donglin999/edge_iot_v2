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

# NOTE: `plc` (Mitsubishi MC protocol, registered as "mc"/"plc") is
# deliberately NOT imported here yet. tests/test_e2e_all_protocols.py::
# test_every_user_facing_protocol_has_a_script asserts the production,
# user-facing protocol list (describe_all()) is exactly the modules imported
# below, and documents "mc"/"plc" as intentionally absent from it. This
# change round (rank5) only fixes plc.py's own METADATA/FieldSpec/IDENTITY_
# FIELDS so the class is correct *whenever* it is registered/imported
# directly (as the existing tests in test_default_read_batch.py and
# test_xiu2_critical_fixes.py already do) — it does not flip on production
# visibility, which is a separate decision (would also need a SCRIPTS entry
# in test_e2e_all_protocols.py) left to whoever owns that call.
# from . import plc   # mc / plc (Mitsubishi MC protocol) — see note above

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
