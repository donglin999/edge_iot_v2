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
_S7_DATA_TYPES = (
    "bool", "byte", "int16", "uint16", "int32", "uint32",
    "float32", "float64", "lreal", "string",
)
#: data_type spellings that mean the 8-byte IEEE-754 double (LReal on
#: S7-1200/1500). Both are accepted since engineers commonly say "LReal".
_DOUBLE_TYPES = ("float64", "lreal")


_AREA_RE = re.compile(
    r"""^
    (?:DB(?P<db>\d+)\.)?
    (?P<area>DBX|DBB|DBW|DBD|MX|MB|MW|MD|IX|IB|IW|ID|QX|QB|QW|QD|M|I|Q)
    (?P<byte>\d+)
    (?:\.(?P<bit>\d+))?
    $""",
    re.IGNORECASE | re.VERBOSE,
)


def _parse_s7_address(address: str, data_type: str = "") -> Tuple[str, int, int, int, int]:
    """Return ``(area, db_number, start_byte, bit_offset, length)``.

    ``area`` is the snap7 ``Area`` enum value.  ``length`` is in bytes for
    everything except booleans (which read 1 byte and mask the bit).

    ``data_type`` is an optional hint used only for the 32-bit "D" (double
    word) address forms — ``DBD``/``MD``/``ID``/``QD``. Normally these are
    4-byte reads (int32/uint32/float32), but when ``data_type`` names an
    8-byte double (``float64``/``lreal``, e.g. S7-1200/1500 LReal) the read
    length is widened to 8 bytes at the same start byte.
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

    # A byte holds 8 bits; a ``.bit`` offset outside 0..7 is a malformed
    # address (e.g. "DBX10.8"). Reject it here rather than silently masking
    # the wrong bit — ``1 << bit`` for bit>7 just yields 0 in _decode().
    if bit > 7:
        raise ValueError(f"S7 位偏移 {bit} 超出范围 0-7: {address!r}")

    dword_len = 8 if (data_type or "").lower() in _DOUBLE_TYPES else 4

    # Map area → snap7.Area + size
    if code in ("DBX",):
        return Area.DB, db, byte, bit, 1
    if code in ("DBB",):
        return Area.DB, db, byte, 0, 1
    if code in ("DBW",):
        return Area.DB, db, byte, 0, 2
    if code in ("DBD",):
        return Area.DB, db, byte, 0, dword_len
    if code in ("M", "MX"):
        return Area.MK, 0, byte, bit, 1
    if code == "MB":
        return Area.MK, 0, byte, 0, 1
    if code == "MW":
        return Area.MK, 0, byte, 0, 2
    if code == "MD":
        return Area.MK, 0, byte, 0, dword_len
    if code in ("I", "IX"):
        return Area.PE, 0, byte, bit, 1
    if code == "IB":
        return Area.PE, 0, byte, 0, 1
    if code == "IW":
        return Area.PE, 0, byte, 0, 2
    if code == "ID":
        return Area.PE, 0, byte, 0, dword_len
    if code in ("Q", "QX"):
        return Area.PA, 0, byte, bit, 1
    if code == "QB":
        return Area.PA, 0, byte, 0, 1
    if code == "QW":
        return Area.PA, 0, byte, 0, 2
    if code == "QD":
        return Area.PA, 0, byte, 0, dword_len
    raise ValueError(f"未识别的 S7 区域 {code}")


#: Fallback STRING capacity (characters, excluding the 2-byte header) when a
#: point doesn't declare ``str_length``. Matches the ``str_length`` FieldSpec
#: default on ``POINT_FIELDS`` below.
_DEFAULT_STR_LENGTH = 32


def _string_read_length(str_length: Any) -> int:
    """Wire length in bytes for a STRING point of declared capacity ``str_length``.

    Siemens S7 STRING on the wire is ``[max_len(1B)][actual_len(1B)][chars...]``
    — always 2 header bytes plus the *declared* character capacity. This is
    intentionally independent of which numeric-address suffix (DBB/DBW/DBD/
    MB/MW/MD/...) the point happens to be typed with: that suffix used to be
    the sole thing deciding read length (1/2/4 bytes), which silently
    truncated every STRING point to 0-2 usable characters regardless of the
    PLC-side declared capacity. See ``_parse_s7_address``'s call site in
    :meth:`SiemensS7Protocol.read_points`, which overrides the address-form
    length with this value whenever ``data_type == "string"``.

    Falls back to :data:`_DEFAULT_STR_LENGTH` for missing/unparseable input;
    never goes negative (a declared length of 0 is legal — an always-empty
    STRING still has to read its 2 header bytes).
    """
    try:
        n = int(str_length)
    except (TypeError, ValueError):
        n = _DEFAULT_STR_LENGTH
    if n < 0:
        n = 0
    return 2 + n


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
    if dt in _DOUBLE_TYPES:
        return struct.unpack(">d", buf)[0]
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
        FieldSpec("plc_type", "PLC 型号", kind="enum", choices=_PLC_TYPES, default="S7-1200",
                  help_text="S7-200 经典款走 TSAP 而非 rack/slot;当前 rack/slot 连接方式主要适配 "
                            "S7-300/400/1200/1500"),
        FieldSpec("timeout", "超时(秒)", kind="float", default=5.0),
    )
    IDENTITY_FIELDS = ("source_ip", "rack", "slot")

    POINT_FIELDS = (
        FieldSpec("code", "测点编码", required=True, example="motor_speed"),
        FieldSpec("address", "地址", required=True,
                  help_text="DB1.DBD0 / DB10.DBX2.3 / MW100 / I0.0 / QW20", example="DB1.DBD0"),
        FieldSpec("data_type", "数据类型", kind="enum", choices=_S7_DATA_TYPES,
                  default="float32",
                  help_text="float64/lreal 用于 S7-1200/1500 的 LReal(8 字节,地址仍填 DBD 起始字节); "
                            "string 的读取长度由 str_length 字段决定,与地址写的是 DBB/DBW/DBD 无关"),
        FieldSpec("str_length", "字符串长度", kind="int", default=32,
                  help_text="仅 data_type=string 时生效:声明的字符容量(不含头部),对应 PLC 侧 "
                            "STRING[str_length] 的声明长度;实际会从地址起始字节读取 2+str_length "
                            "字节(西门子 STRING 格式:[max_len(1B)][actual_len(1B)][chars...])"),
        FieldSpec("unit", "单位", default=""),
        FieldSpec("description", "中文名称", default=""),
        FieldSpec("coefficient", "系数", kind="float", default=1.0),
    )

    def __init__(self, device_config: Dict[str, Any]) -> None:
        super().__init__(device_config)
        self.ip = device_config.get("source_ip")
        self.rack = int(device_config.get("rack", 0))
        self.slot = int(device_config.get("slot", 1))
        self.port = int(device_config.get("source_port", 102) or 102)
        self.timeout = float(device_config.get("timeout", 5.0))
        self.client = None

    def connect(self) -> bool:
        if not _SNAP7_AVAILABLE:
            raise ConnectionError("python-snap7 未安装,无法连接 S7 PLC")
        try:
            self.client = snap7.client.Client()
            self.client.set_connection_type(3)  # OP connection (default for HMI)
            # NOTE: source_port is device-configurable (FieldSpec default 102)
            # but was never actually threaded through here — every connect()
            # silently hit the hardcoded snap7 default (102) regardless of
            # what the device row said. Real PLCs almost always sit on 102,
            # but simulators / NAT port-forwards that expose a different
            # port were unreachable with no error explaining why.
            self.client.connect(self.ip, self.rack, self.slot, self.port)
            self.is_connected = self.client.get_connected()
            if self.is_connected:
                self.logger.info("Connected to S7 PLC %s:%d rack=%d slot=%d", self.ip, self.port, self.rack, self.slot)
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
        errors: List[str] = []
        for point in points:
            try:
                data_type = point.get("data_type", "uint16")
                area, db, byte, bit, length = _parse_s7_address(
                    str(point.get("address", "")), data_type
                )
                if (data_type or "").lower() == "string":
                    # STRING read length is governed by the declared capacity,
                    # not by the DBB/DBW/DBD suffix _parse_s7_address derived
                    # ``length`` from — see _string_read_length().
                    length = _string_read_length(point.get("str_length", _DEFAULT_STR_LENGTH))
                buf = self.client.read_area(area, db, byte, length)
                value = _decode(bytes(buf), data_type, bit)
                results.append({
                    "code": point["code"],
                    "value": value,
                    "timestamp": time.time_ns(),
                    "quality": "good",
                    "address": point.get("address"),
                })
            except Exception as exc:  # noqa: BLE001
                # A single bad address / decode must not discard the whole
                # batch: flag just this point as bad-quality and carry on so
                # its healthy peers still produce readings.
                msg = f"{point.get('code')} @ {point.get('address')}: {exc}"
                errors.append(msg)
                self.logger.warning("S7 read failed for %s", msg)
                results.append({
                    "code": point["code"],
                    "value": None,
                    "timestamp": time.time_ns(),
                    "quality": "bad",
                    "address": point.get("address"),
                })
        # If *every* point failed the device itself is unreachable — raise so
        # the caller treats it as a transport failure and triggers reconnect,
        # rather than silently streaming an all-bad batch forever.
        if points and len(errors) == len(points):
            raise ReadError(f"S7 read failed for all {len(points)} point(s): {errors[0]}")
        return results
