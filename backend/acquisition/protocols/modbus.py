"""Modbus TCP and Modbus RTU protocol implementations.

The two transports share an enormous amount of logic (point grouping,
function-code routing, multi-register combining), so we put everything in a
common :class:`_ModbusBase` and only the connection wiring differs between
:class:`ModbusTCPProtocol` and :class:`ModbusRTUProtocol`.

Field schemas (see ``base.FieldSpec``) are declared on each subclass so the
importer / API can validate per-row before any device connection is opened.
"""
from __future__ import annotations

import re
import socket
import struct
import time
from typing import Any, Dict, List

import modbus_tk.defines as cst
from modbus_tk import modbus_rtu, modbus_tcp

from acquisition.services.read_plan import (
    PointMeta,
    ReadGroup,
    ReadPlanBuilder,
    Reading,
)

from .base import (
    BaseProtocol,
    ConnectionError,
    FieldSpec,
    ProtocolMeta,
    ProtocolRegistry,
    ReadError,
)


# Function codes (Modbus standard) — exposed as enum choices on the FieldSpec.
_FUNCTION_CODES = (
    1,   # Read Coils
    2,   # Read Discrete Inputs
    3,   # Read Holding Registers (default)
    4,   # Read Input Registers
)
_DATA_TYPES = (
    "bool",
    "int16", "uint16",
    "int32", "uint32",
    "int64", "uint64",
    "float32", "float64",
)
_BYTE_ORDERS = ("big", "little", "big-swap", "little-swap")


# ---------------------------------------------------------------------------
# register combination
# ---------------------------------------------------------------------------


# A multi-register scalar (2 or 4 uint16 words) is decoded by composing two
# independent transforms that together cover all four ``byte_order`` modes:
#   * within-word byte swap — needed only for the *-swap orders (BADC / CDAB)
#   * struct endianness char — ">" for big/big-swap, "<" for little/little-swap
# Swapping the bytes inside each word (when required) and then unpacking with
# the matching endianness char reproduces the device's value for every order;
# this is why _combine_registers must NOT hard-code ">f".
_BYTE_SWAP_ORDERS = frozenset({"big-swap", "little-swap"})
_LITTLE_ENDIAN_ORDERS = frozenset({"little", "little-swap"})


def _apply_byte_order(word_bytes: bytes, byte_order: str) -> bytes:
    """Swap the two bytes inside each 16-bit word when ``byte_order`` requires it.

    ``big`` (ABCD) / ``little`` (DCBA) keep each word's bytes as received — the
    word-level ordering is then expressed through the struct endianness char
    (see :func:`_struct_prefix`). ``big-swap`` (BADC) / ``little-swap`` (CDAB)
    additionally need the two bytes inside every word swapped.

    Always returns a freshly joined ``bytes`` object (never an iterator).
    """
    bo = (byte_order or "big").strip().lower()
    if bo not in _BYTE_SWAP_ORDERS:
        return bytes(word_bytes)
    words = [word_bytes[i : i + 2] for i in range(0, len(word_bytes), 2)]
    return b"".join(bytes(reversed(w)) for w in words)


def _struct_prefix(byte_order: str) -> str:
    """Return the ``struct`` endianness char (``>`` or ``<``) for ``byte_order``."""
    bo = (byte_order or "big").strip().lower()
    return "<" if bo in _LITTLE_ENDIAN_ORDERS else ">"


def _combine_registers(registers, data_type: str, num: int, byte_order: str = "big"):
    """Combine N consecutive uint16 registers into the right scalar.

    ``byte_order`` is honoured for every multi-register type: bytes are
    rearranged by :func:`_apply_byte_order` and the ``struct`` format string is
    selected via :func:`_struct_prefix` so big- and little-endian devices both
    decode correctly.
    """
    if num <= 1:
        if not registers:
            return 0
        return bool(registers[0]) if (data_type or "").lower() == "bool" else registers[0]

    dt = (data_type or "").strip().lower()
    word_bytes = b"".join(
        int(r).to_bytes(2, byteorder="big", signed=False) for r in registers[:num]
    )
    word_bytes = _apply_byte_order(word_bytes, byte_order)
    pfx = _struct_prefix(byte_order)

    if num == 2 and dt in ("float", "float32", "real"):
        return struct.unpack(pfx + "f", word_bytes)[0]
    if num == 4 and dt in ("float64", "double"):
        return struct.unpack(pfx + "d", word_bytes)[0]
    if num == 2 and dt in ("int32", "long", "dint"):
        return struct.unpack(pfx + "i", word_bytes)[0]
    if num == 2 and dt in ("uint32", "udint", "dword"):
        return struct.unpack(pfx + "I", word_bytes)[0]
    if num == 4 and dt in ("int64", "lint"):
        return struct.unpack(pfx + "q", word_bytes)[0]
    if num == 4 and dt in ("uint64", "ulint", "qword"):
        return struct.unpack(pfx + "Q", word_bytes)[0]

    return list(registers[:num])


# ---------------------------------------------------------------------------
# Legacy read_points() adapters
#
# The dict-based read_points() path predates ReadPlanBuilder. These tiny
# duck-typed stand-ins let it reuse the exact same plan builder the pipeline
# uses, without importing the Django-bound pipeline module.
# ---------------------------------------------------------------------------


class _LegacyPoint:
    """Wrap a legacy point dict so :class:`ReadPlanBuilder` sees the
    attributes (``code``, ``address``, ``extra``, ``template``) it expects."""

    __slots__ = ("code", "address", "extra", "template")

    def __init__(self, raw: Dict[str, Any]) -> None:
        self.code = raw["code"]
        self.address = raw.get("address", "0")
        # ReadPlanBuilder pulls function_code / data_type / type / num here.
        self.extra = {k: v for k, v in raw.items() if k not in ("code", "address")}
        self.template = None


class _LegacyDevice:
    """Minimal device stand-in carrying just what ``ReadPlanBuilder`` reads."""

    __slots__ = ("protocol", "metadata")

    def __init__(self, protocol: str, slave_id: int) -> None:
        self.protocol = protocol
        self.metadata = {"slave_id": slave_id}


# ---------------------------------------------------------------------------
# Common base
# ---------------------------------------------------------------------------


class _ModbusBase(BaseProtocol):
    """Logic shared by Modbus TCP and Modbus RTU."""

    POINT_FIELDS = (
        FieldSpec("code", "测点编码", required=True, help_text="测点唯一标识,如 'temp_01'", example="temp_01"),
        FieldSpec("address", "寄存器地址", required=True,
                  help_text="支持 0-based 直接地址、SCADA 风格 (D100/I101/C001/T102) 或 4xxxx/3xxxx", example="40001"),
        FieldSpec("function_code", "功能码", kind="enum", choices=_FUNCTION_CODES, default=3,
                  help_text="3=保持寄存器(默认), 4=输入寄存器, 1=线圈, 2=离散输入"),
        FieldSpec("data_type", "数据类型", kind="enum", choices=_DATA_TYPES, default="uint16",
                  help_text="决定多寄存器组合规则;uint16/int16=1 个寄存器, float32/int32=2 个, float64/int64=4 个"),
        FieldSpec("num", "寄存器数量", kind="int", default=1,
                  help_text="float32 填 2, float64 填 4, 否则一般为 1", example=1),
        FieldSpec("unit", "单位", default="", example="℃"),
        FieldSpec("description", "中文名称", example="温度"),
        FieldSpec("coefficient", "系数", kind="float", default=1.0, help_text="原始值乘以该系数"),
    )

    def __init__(self, device_config: Dict[str, Any]) -> None:
        super().__init__(device_config)
        self.slave_addr = int(device_config.get("slave_id", device_config.get("source_slave_addr", 1)))
        self.timeout = float(device_config.get("timeout", 10))
        self.byte_order = str(device_config.get("byte_order", "big"))
        self.master = None

    # subclasses build self.master in connect()
    def disconnect(self) -> None:
        if self.master:
            try:
                self.master.close()
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("Modbus disconnect error: %s", exc)
            finally:
                self.master = None
                self.is_connected = False

    def health_check(self) -> bool:
        if not self.is_connected or not self.master:
            return False
        try:
            self.master.execute(
                slave=self.slave_addr,
                function_code=cst.READ_HOLDING_REGISTERS,
                starting_address=0,
                quantity_of_x=1,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Modbus health check failed: %s", exc)
            return False

    def read_points(self, points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Legacy dict-in / dict-out read interface.

        This now delegates to the same gap-tolerant :class:`ReadPlanBuilder`
        coalescing and :meth:`read_batch` decoding the pipeline uses, so the
        old call path benefits from batch merging (fewer round-trips) and
        per-point error isolation instead of the previous strict-contiguous
        grouping. The dict result shape is preserved for existing callers.
        """
        if not self.is_connected or not self.master:
            if not self.connect():
                raise ReadError("Not connected to Modbus device")

        groups = ReadPlanBuilder.build(
            _LegacyDevice(self.META.name, self.slave_addr),
            [_LegacyPoint(p) for p in points],
        )
        addr_by_code = {pm.code: pm.address for g in groups for pm in g.points}

        results: List[Dict[str, Any]] = []
        for group in groups:
            for reading in self.read_batch(group):
                results.append({
                    "code": reading.point_code,
                    "value": reading.value,
                    "timestamp": reading.timestamp_ns,
                    "quality": reading.quality,
                    "address": addr_by_code.get(reading.point_code),
                    "raw_data": reading.raw,
                })
        return results

    # ------- Batched read (new pipeline path) ------- #
    def read_batch(self, group: ReadGroup) -> List[Reading]:
        """Execute one :class:`ReadGroup` against the device.

        On a transport-level failure the whole call raises :class:`ReadError`
        — the pipeline is responsible for reconnect / retry. Per-point decode
        errors do *not* bubble up: the offending point gets a
        ``quality="bad"`` reading and its peers in the same group still
        succeed.
        """
        if not self.is_connected or not self.master:
            if not self.connect():
                raise ReadError("Not connected to Modbus device")

        if group.function_code is None or group.start_address is None or group.register_count is None:
            raise ReadError(
                "ReadGroup missing modbus fields (function_code/start_address/register_count)"
            )

        try:
            data = self.master.execute(
                slave=self.slave_addr,
                function_code=group.function_code,
                starting_address=group.start_address,
                quantity_of_x=group.register_count,
            )
        except Exception as exc:  # noqa: BLE001
            raise ReadError(
                f"Failed to read fc={group.function_code} @ {group.start_address} "
                f"len={group.register_count}: {exc}"
            ) from exc

        now_ns = time.time_ns()
        readings: List[Reading] = []
        for pm in group.points:
            offset = pm.address - group.start_address
            num = pm.num_registers
            if offset < 0 or offset + num > len(data):
                self.logger.warning(
                    "Point %s out of range in group (offset=%d num=%d len=%d)",
                    pm.code, offset, num, len(data),
                )
                readings.append(
                    Reading(point_code=pm.code, value=None, timestamp_ns=now_ns, quality="bad")
                )
                continue

            slice_ = data[offset : offset + num]
            try:
                value = _combine_registers(slice_, pm.data_type, num, self.byte_order)
                readings.append(
                    Reading(
                        point_code=pm.code,
                        value=value,
                        timestamp_ns=now_ns,
                        quality="good",
                        raw=list(slice_),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("Decode failed for %s: %s", pm.code, exc)
                readings.append(
                    Reading(point_code=pm.code, value=None, timestamp_ns=now_ns, quality="bad")
                )
        return readings

    # --- point normalisation --- #
    def _group_continuous_registers(
        self, points: List[Dict[str, Any]]
    ) -> Dict[int, List[List[Dict[str, Any]]]]:
        function_groups: Dict[int, List[Dict[str, Any]]] = {}
        for point in points:
            func_code_raw = point.get("function_code", point.get("type", 3))
            data_type = ""
            try:
                func_code = int(func_code_raw)
                if isinstance(point.get("type"), str) and not str(point.get("type")).strip().isdigit():
                    data_type = str(point.get("type"))
                if isinstance(point.get("data_type"), str):
                    data_type = str(point.get("data_type"))
            except (ValueError, TypeError):
                func_code = 3
                data_type = str(func_code_raw)

            try:
                num = int(point.get("num", 1))
            except (TypeError, ValueError):
                num = 1

            actual_addr = self._parse_address(str(point.get("address", "0")))

            normalized = {
                "code": point["code"],
                "address": actual_addr,
                "num": num,
                "function_code": func_code,
                "data_type": data_type,
            }
            function_groups.setdefault(func_code, []).append(normalized)

        for fc in function_groups:
            function_groups[fc].sort(key=lambda x: x["address"])

        continuous: Dict[int, List[List[Dict[str, Any]]]] = {}
        for fc, regs in function_groups.items():
            grouped: List[List[Dict[str, Any]]] = []
            current: List[Dict[str, Any]] = []
            for reg in regs:
                if not current:
                    current.append(reg)
                    continue
                expected = current[-1]["address"] + current[-1]["num"]
                if reg["address"] == expected:
                    current.append(reg)
                else:
                    grouped.append(current)
                    current = [reg]
            if current:
                grouped.append(current)
            continuous[fc] = grouped
        return continuous

    @staticmethod
    def _parse_address(addr_str: str) -> int:
        """Convert a user-supplied register address into a 0-based offset.

        Rules:
        * SCADA prefixes ``D<n>`` / ``I<n>`` / ``C<n>`` / ``T<n>`` — ``<n>`` is
          taken as a 0-based offset within the area (D=holding, I=input,
          C=coil, T=discrete input). The function code is selected by the
          caller via ``function_code`` / ``type``.
        * Modbus *display* addresses ``40001..49999`` / ``30001..39999`` /
          ``10001..19999`` are 1-based historical conventions — convert by
          subtracting the area base.
        * Plain integers (``0``, ``1``, ``"42"``, ...) are passed through
          unchanged: this is the modern 0-based convention used on the wire,
          which is what every Modbus library and most templates use.
        """
        addr_str = addr_str.strip()
        prefix = re.match(r"^([DICT])(\d+)$", addr_str, re.IGNORECASE)
        if prefix:
            return int(prefix.group(2))
        try:
            raw = int(addr_str)
        except ValueError:
            return 0
        if raw >= 40001:
            return raw - 40001
        if raw >= 30001:
            return raw - 30001
        if raw >= 10001:
            return raw - 10001
        return raw


# ---------------------------------------------------------------------------
# Modbus TCP
# ---------------------------------------------------------------------------


@ProtocolRegistry.register("modbus_tcp", "modbustcp", "modbus")
class ModbusTCPProtocol(_ModbusBase):
    META = ProtocolMeta(
        name="modbus_tcp",
        label="Modbus TCP",
        category="industrial-ethernet",
        description="基于以太网的 Modbus(端口 502),支持保持/输入寄存器、线圈、离散量",
    )

    DEVICE_FIELDS = (
        FieldSpec("source_ip", "IP 地址", required=True, example="192.168.1.100"),
        FieldSpec("source_port", "端口", kind="int", default=502, example=502),
        # 「从站/Slave」是 RS-485 串行多点总线的概念;Modbus TCP 帧里对应的是
        # MBAP 头的 Unit Identifier(单元标识符)。纯 TCP 设备直连一般填 1,经
        # TCP→RTU 网关时才填后端串行从机地址。叫「从站地址」会让工程师误按串口站号规划。
        FieldSpec("slave_id", "单元标识符 (Unit ID)", kind="int", default=1,
                  help_text="Modbus TCP 单元标识符(MBAP Unit Id);直连设备一般填 1,经 TCP/RTU 网关时填后端串行从机地址"),
        FieldSpec("byte_order", "字节序", kind="enum", choices=_BYTE_ORDERS, default="big",
                  help_text="float32/int32 多字节解析顺序;ABCD=big, DCBA=little, BADC=big-swap, CDAB=little-swap"),
        FieldSpec("timeout", "超时(秒)", kind="float", default=10.0),
    )
    IDENTITY_FIELDS = ("source_ip", "source_port", "slave_id")

    def __init__(self, device_config: Dict[str, Any]) -> None:
        super().__init__(device_config)
        self.ip = device_config.get("source_ip")
        self.port = int(device_config.get("source_port", 502))

    def connect(self) -> bool:
        try:
            self.master = modbus_tcp.TcpMaster(host=self.ip, port=self.port, timeout_in_sec=self.timeout)
            # Open the socket eagerly so we can tune it; modbus-tk would
            # otherwise lazy-open on the first execute().
            self.master.open()
            self._enable_keepalive()
            self.is_connected = True
            self.logger.info("Connected to Modbus TCP %s:%s slave=%s", self.ip, self.port, self.slave_addr)
            return True
        except Exception as exc:  # noqa: BLE001
            self.is_connected = False
            raise ConnectionError(f"Modbus TCP connection failed: {exc}") from exc

    def _enable_keepalive(self) -> None:
        """Enable TCP keep-alive on the Modbus socket.

        Without ``SO_KEEPALIVE`` a silently dropped link (cable pull, switch
        reboot, NAT idle-timeout) is only noticed when the next read times
        out — which at long poll intervals can be minutes. Keep-alive probes
        surface the dead peer proactively. Probe-interval knobs are set
        best-effort: they are not portable (e.g. macOS lacks
        ``TCP_KEEPIDLE``), so each is guarded by ``getattr``.
        """
        sock = getattr(self.master, "_sock", None)
        if sock is None:
            return
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for opt_name, value in (
                ("TCP_KEEPIDLE", 30),   # idle seconds before first probe
                ("TCP_KEEPINTVL", 10),  # seconds between probes
                ("TCP_KEEPCNT", 3),     # failed probes before drop
            ):
                opt = getattr(socket, opt_name, None)
                if opt is not None:
                    sock.setsockopt(socket.IPPROTO_TCP, opt, value)
        except OSError as exc:
            self.logger.warning("Failed to enable SO_KEEPALIVE on Modbus TCP socket: %s", exc)


# ---------------------------------------------------------------------------
# Modbus RTU
# ---------------------------------------------------------------------------


@ProtocolRegistry.register("modbus_rtu", "modbusrtu", "rtu")
class ModbusRTUProtocol(_ModbusBase):
    META = ProtocolMeta(
        name="modbus_rtu",
        label="Modbus RTU",
        category="fieldbus",
        description="基于串口(RS-485/RS-232)的 Modbus,常见于工业现场总线",
    )

    DEVICE_FIELDS = (
        FieldSpec("serial_port", "串口设备", required=True,
                  help_text="如 /dev/ttyUSB0 或 COM3", example="/dev/ttyUSB0"),
        FieldSpec("baudrate", "波特率", kind="int", default=9600,
                  help_text="常见值: 9600 / 19200 / 38400 / 115200"),
        FieldSpec("parity", "校验位", kind="enum", choices=("N", "E", "O"), default="N",
                  help_text="N=无, E=偶校验, O=奇校验"),
        FieldSpec("bytesize", "数据位", kind="enum", choices=(8,), default=8,
                  help_text="RTU 帧固定 8 数据位;7 位数据位是 Modbus ASCII 模式,RTU 不支持"),
        FieldSpec("stopbits", "停止位", kind="enum", choices=(1, 2), default=1),
        FieldSpec("slave_id", "从站地址", kind="int", required=True, example=1,
                  help_text="RS-485 总线上每台从机的唯一地址 1-247"),
        FieldSpec("byte_order", "字节序", kind="enum", choices=_BYTE_ORDERS, default="big"),
        FieldSpec("timeout", "超时(秒)", kind="float", default=2.0),
    )
    IDENTITY_FIELDS = ("serial_port", "slave_id")

    def __init__(self, device_config: Dict[str, Any]) -> None:
        super().__init__(device_config)
        self.serial_port = device_config.get("serial_port")
        self.baudrate = int(device_config.get("baudrate", 9600))
        self.parity = str(device_config.get("parity", "N"))
        self.bytesize = int(device_config.get("bytesize", 8))
        self.stopbits = int(device_config.get("stopbits", 1))

    def connect(self) -> bool:
        try:
            import serial  # local import — only RTU users pay the dep cost

            ser = serial.Serial(
                port=self.serial_port,
                baudrate=self.baudrate,
                parity=self.parity,
                bytesize=self.bytesize,
                stopbits=self.stopbits,
                timeout=self.timeout,
            )
            self.master = modbus_rtu.RtuMaster(ser)
            self.master.set_timeout(self.timeout)
            self.is_connected = True
            self.logger.info(
                "Connected to Modbus RTU %s @ %d baud slave=%s",
                self.serial_port, self.baudrate, self.slave_addr,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            self.is_connected = False
            raise ConnectionError(f"Modbus RTU connection failed: {exc}") from exc
