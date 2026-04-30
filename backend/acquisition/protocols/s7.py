"""Siemens S7 (S7-200/300/400/1200/1500) protocol implementation.

Uses python-snap7 which talks the ISO-on-TCP / S7Comm protocol (port 102).
Address syntax for points follows the standard S7 area notation:

    DB<n>.DBX<byte>.<bit>     bit in DB<n>           bool
    DB<n>.DBB<byte>           byte in DB<n>          uint8/int8
    DB<n>.DBW<byte>           word in DB<n>          int16/uint16
    DB<n>.DBD<byte>           dword in DB<n>         int32/uint32/float32
    M<byte>.<bit> / MW<n>     merker (flag) area     bool / int16
    I<byte>.<bit> / IW<n>     inputs (PE/PI)
    Q<byte>.<bit> / QW<n>     outputs (PA/PQ)
"""
from __future__ import annotations

import re
import struct
import time
from typing import Any, Dict, List, Tuple

from .base import (
    BaseProtocol,
    ConnectionError,
    FieldSpec,
    ProtocolMeta,
    ProtocolRegistry,
    ReadError,
)

try:
    import snap7
    from snap7.type import Area
    _SNAP7_AVAILABLE = True
except ImportError:  # pragma: no cover - graceful fallback
    snap7 = None
    Area = None
    _SNAP7_AVAILABLE = False


_PLC_TYPES = ("S7-200", "S7-300", "S7-400", "S7-1200", "S7-1500")
_S7_DATA_TYPES = ("bool", "byte", "int16", "uint16", "int32", "uint32", "float32", "string")


_AREA_RE = re.compile(
    r"""^
    (?:DB(?P<db>\d+)\.)?
    (?P<area>DBX|DBB|DBW|DBD|MX|MB|MW|MD|IX|IB|IW|ID|QX|QB|QW|QD|M|I|Q)
    (?P<byte>\d+)
    (?:\.(?P<bit>\d+))?
    $""",
    re.IGNORECASE | re.VERBOSE,
)


def _parse_s7_address(address: str) -> Tuple[str, int, int, int, int]:
    """Return ``(area, db_number, start_byte, bit_offset, length)``.

    ``area`` is the snap7 ``Area`` enum value.  ``length`` is in bytes for
    everything except booleans (which read 1 byte and mask the bit).
    """
    if not _SNAP7_AVAILABLE:
        raise RuntimeError("python-snap7 not installed")

    m = _AREA_RE.match(address.strip())
    if not m:
        raise ValueError(f"无法解析 S7 地址 {address!r}")
    parts = m.groupdict()
    db = int(parts["db"] or 0)
    code = parts["area"].upper()
    byte = int(parts["byte"])
    bit = int(parts["bit"] or 0)

    # Map area → snap7.Area + size
    if code in ("DBX",):
        return Area.DB, db, byte, bit, 1
    if code in ("DBB",):
        return Area.DB, db, byte, 0, 1
    if code in ("DBW",):
        return Area.DB, db, byte, 0, 2
    if code in ("DBD",):
        return Area.DB, db, byte, 0, 4
    if code in ("M", "MX"):
        return Area.MK, 0, byte, bit, 1
    if code == "MB":
        return Area.MK, 0, byte, 0, 1
    if code == "MW":
        return Area.MK, 0, byte, 0, 2
    if code == "MD":
        return Area.MK, 0, byte, 0, 4
    if code in ("I", "IX"):
        return Area.PE, 0, byte, bit, 1
    if code == "IB":
        return Area.PE, 0, byte, 0, 1
    if code == "IW":
        return Area.PE, 0, byte, 0, 2
    if code == "ID":
        return Area.PE, 0, byte, 0, 4
    if code in ("Q", "QX"):
        return Area.PA, 0, byte, bit, 1
    if code == "QB":
        return Area.PA, 0, byte, 0, 1
    if code == "QW":
        return Area.PA, 0, byte, 0, 2
    if code == "QD":
        return Area.PA, 0, byte, 0, 4
    raise ValueError(f"未识别的 S7 区域 {code}")


def _decode(buf: bytes, data_type: str, bit: int) -> Any:
    dt = (data_type or "").lower()
    if dt == "bool":
        return bool(buf[0] & (1 << bit))
    if dt in ("int16",):
        return struct.unpack(">h", buf)[0]
    if dt in ("uint16",):
        return struct.unpack(">H", buf)[0]
    if dt in ("int32",):
        return struct.unpack(">i", buf)[0]
    if dt in ("uint32",):
        return struct.unpack(">I", buf)[0]
    if dt in ("float32", "real"):
        return struct.unpack(">f", buf)[0]
    if dt == "byte":
        return buf[0]
    if dt == "string":
        # Siemens S7 string: byte 0=max len, byte 1=actual len, bytes 2..n=chars
        if len(buf) < 2:
            return ""
        actual = buf[1]
        return buf[2 : 2 + actual].decode("utf-8", errors="replace")
    return list(buf)


@ProtocolRegistry.register("siemens_s7", "s7")
class SiemensS7Protocol(BaseProtocol):
    META = ProtocolMeta(
        name="siemens_s7",
        label="西门子 S7",
        category="industrial-ethernet",
        description="ISO-on-TCP (端口 102),支持 S7-200/300/400/1200/1500 系列;按 DB/M/I/Q 区域寻址",
    )

    DEVICE_FIELDS = (
        FieldSpec("source_ip", "PLC IP", required=True, example="192.168.1.10"),
        FieldSpec("source_port", "端口", kind="int", default=102),
        FieldSpec("rack", "Rack 号", kind="int", default=0,
                  help_text="S7-1200/1500 一般为 0"),
        FieldSpec("slot", "Slot 号", kind="int", default=1,
                  help_text="S7-1200/1500 一般为 1, S7-300/400 一般为 2"),
        FieldSpec("plc_type", "PLC 型号", kind="enum", choices=_PLC_TYPES, default="S7-1200"),
        FieldSpec("timeout", "超时(秒)", kind="float", default=5.0),
    )
    IDENTITY_FIELDS = ("source_ip", "rack", "slot")

    POINT_FIELDS = (
        FieldSpec("code", "测点编码", required=True, example="motor_speed"),
        FieldSpec("address", "地址", required=True,
                  help_text="DB1.DBD0 / DB10.DBX2.3 / MW100 / I0.0 / QW20", example="DB1.DBD0"),
        FieldSpec("data_type", "数据类型", kind="enum", choices=_S7_DATA_TYPES,
                  default="float32"),
        FieldSpec("unit", "单位", default=""),
        FieldSpec("description", "中文名称", default=""),
        FieldSpec("coefficient", "系数", kind="float", default=1.0),
    )

    def __init__(self, device_config: Dict[str, Any]) -> None:
        super().__init__(device_config)
        self.ip = device_config.get("source_ip")
        self.rack = int(device_config.get("rack", 0))
        self.slot = int(device_config.get("slot", 1))
        self.timeout = float(device_config.get("timeout", 5.0))
        self.client = None

    def connect(self) -> bool:
        if not _SNAP7_AVAILABLE:
            raise ConnectionError("python-snap7 未安装,无法连接 S7 PLC")
        try:
            self.client = snap7.client.Client()
            self.client.set_connection_type(3)  # OP connection (default for HMI)
            self.client.connect(self.ip, self.rack, self.slot)
            self.is_connected = self.client.get_connected()
            if self.is_connected:
                self.logger.info("Connected to S7 PLC %s rack=%d slot=%d", self.ip, self.rack, self.slot)
            return self.is_connected
        except Exception as exc:  # noqa: BLE001
            self.is_connected = False
            raise ConnectionError(f"S7 connection failed: {exc}") from exc

    def disconnect(self) -> None:
        if self.client:
            try:
                self.client.disconnect()
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("S7 disconnect error: %s", exc)
            finally:
                self.client = None
                self.is_connected = False

    def health_check(self) -> bool:
        return bool(self.client and self.client.get_connected())

    def read_points(self, points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not self.is_connected:
            if not self.connect():
                raise ReadError("Not connected to S7 PLC")

        results: List[Dict[str, Any]] = []
        for point in points:
            try:
                area, db, byte, bit, length = _parse_s7_address(str(point.get("address", "")))
                buf = self.client.read_area(area, db, byte, length)
                value = _decode(bytes(buf), point.get("data_type", "uint16"), bit)
                results.append({
                    "code": point["code"],
                    "value": value,
                    "timestamp": time.time_ns(),
                    "quality": "good",
                    "address": point.get("address"),
                })
            except Exception as exc:  # noqa: BLE001
                raise ReadError(
                    f"S7 read failed for {point.get('code')} @ {point.get('address')}: {exc}"
                ) from exc
        return results
