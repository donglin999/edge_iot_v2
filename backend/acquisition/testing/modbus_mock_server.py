"""本地 Modbus TCP mock 服务器 —— 无硬件联调用。

和 simulator 协议不同,这个是**真的 Modbus TCP 服务器**(modbus_tk 的 TcpServer)。
用它当被采对象,采集走的是真正的 ModbusTCPProtocol:真开 TCP、真发功能码、真读
寄存器、真做字节序/多寄存器解码。所以像「float32 被当 uint16 读半个值」这种只在
modbus 解码路径才暴露的 bug,用它才查得出 —— simulator 永远返回 good,查不出来。

保持字节序一致:协议侧默认 big-endian(ABCD)。float32/int32 占 2 个保持寄存器,
按大端拆成高低字写进保持寄存器区,协议读回来解码应还原成原值。
"""
from __future__ import annotations

import socket
import struct
import threading
import time
from typing import Dict, List, Optional, Tuple

import modbus_tk.defines as cst
from modbus_tk import modbus_tcp


def _f32_regs(value: float) -> Tuple[int, int]:
    """float32 → (高位寄存器, 低位寄存器),大端。"""
    hi, lo = struct.unpack(">HH", struct.pack(">f", value))
    return hi, lo


def _i32_regs(value: int) -> Tuple[int, int]:
    hi, lo = struct.unpack(">HH", struct.pack(">i", value))
    return hi, lo


# ---------------------------------------------------------------------------
# Generic scalar -> wire-register encoder (full-datatype test support)
#
# Deliberately independent of ``acquisition.protocols.modbus._combine_registers``
# / ``_apply_byte_order`` — this is the "device" side of the wire, and reusing
# the decoder-under-test's helpers here would let a symmetric bug in both
# cancel itself out silently. Every byte-order transform is reimplemented
# from scratch against ``struct`` so it acts as an independent oracle.
# ---------------------------------------------------------------------------

#: struct format char for each multi-register (2 or 4 word) scalar type.
_ENCODE_FMT = {
    "uint32": "I", "int32": "i", "float32": "f", "float": "f", "real": "f",
    "uint64": "Q", "int64": "q", "float64": "d", "double": "d",
}
_BYTE_SWAP_ORDERS = frozenset({"big-swap", "little-swap"})
_LITTLE_ENDIAN_ORDERS = frozenset({"little", "little-swap"})


def encode_scalar(value, data_type: str, byte_order: str = "big") -> List[int]:
    """Encode ``value`` into the list of 16-bit registers a real device would
    put on the wire for ``data_type`` / ``byte_order``.

    ``bool``/``int16``/``uint16`` occupy a single register (byte order does
    not apply to a lone word — mirrors how the decoder treats ``num <= 1``).
    Multi-register types are packed with ``struct`` at the requested
    endianness, then the inner-word byte swap is applied for the ``*-swap``
    orders (BADC / CDAB).
    """
    dt = (data_type or "").strip().lower()
    if dt == "bool":
        return [1 if value else 0]
    if dt in ("int16", "uint16", "short", "word"):
        return [int(value) & 0xFFFF]

    fmt = _ENCODE_FMT.get(dt)
    if fmt is None:
        raise ValueError(f"encode_scalar: unsupported data_type {data_type!r}")

    bo = (byte_order or "big").strip().lower()
    pfx = "<" if bo in _LITTLE_ENDIAN_ORDERS else ">"
    raw = struct.pack(pfx + fmt, value)
    if bo in _BYTE_SWAP_ORDERS:
        words = [raw[i : i + 2] for i in range(0, len(raw), 2)]
        raw = b"".join(bytes(reversed(w)) for w in words)
    return [int.from_bytes(raw[i : i + 2], "big") for i in range(0, len(raw), 2)]


#: Modbus function code -> block name registered on each slave (see
#: ``ModbusMockServer.__init__``).
_BLOCK_NAME_BY_FC = {1: "coils", 2: "discrete", 3: "holding", 4: "input"}


class ModbusMockServer:
    """在本地端口上跑一个 Modbus TCP 从站,持有一组随时间变化的寄存器。

    寄存器布局(保持寄存器,功能码 3):
        40001 (addr 0)      uint16  温度  —— 20~30 缓慢正弦
        40002 (addr 1)      uint16  湿度  —— 40~60
        40011-40012 (10-11) float32 压力  —— 0.5~1.5 MPa
        40021-40022 (20-21) int32   计数  —— 单调递增
    """

    #: 供采集配置直接引用的测点定义(code, address, data_type, num, unit, desc)。
    POINTS: List[Dict] = [
        {"code": "temperature", "address": "40001", "data_type": "uint16", "num": 1, "unit": "℃", "desc": "温度"},
        {"code": "humidity", "address": "40002", "data_type": "uint16", "num": 1, "unit": "%", "desc": "湿度"},
        {"code": "pressure", "address": "40011", "data_type": "float32", "num": 2, "unit": "MPa", "desc": "压力"},
        {"code": "counter", "address": "40021", "data_type": "int32", "num": 2, "unit": "", "desc": "计数"},
    ]

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 15020,
        slave_ids: List[int] | None = None,
        holding_size: int = 128,
        input_size: int = 64,
        coil_size: int = 64,
        discrete_size: int = 64,
    ) -> None:
        self.host = host
        self.port = port
        self.slave_ids = slave_ids or [1]
        self._server = modbus_tcp.TcpServer(address=host, port=port)
        self._slaves = []
        self._slave_by_id: Dict[int, object] = {}
        for sid in self.slave_ids:
            slave = self._server.add_slave(sid)
            # 保持寄存器区,覆盖到 int32 计数所在的 22 个字(默认 128,给全数据
            # 类型/多点合并测试留够地址空间)。
            slave.add_block("holding", cst.HOLDING_REGISTERS, 0, holding_size)
            # 功能码 4/1/2 各自独立地址空间 —— 供 Agent A 全功能码测试使用,
            # _update_loop 只写 holding,不touch 这几个区。
            slave.add_block("input", cst.ANALOG_INPUTS, 0, input_size)
            slave.add_block("coils", cst.COILS, 0, coil_size)
            slave.add_block("discrete", cst.DISCRETE_INPUTS, 0, discrete_size)
            self._slaves.append(slave)
            self._slave_by_id[sid] = slave
        self._stop = threading.Event()
        self._updater: threading.Thread | None = None

    # ------------------------------------------------------------------
    def start(self, timeout: float = 2.0) -> None:
        """Start the TCP server and return only after its socket accepts.

        ``modbus_tk.TcpServer.start()`` only launches a background thread; its
        bind/listen happens later in that thread.  Callers used to race that
        bind and occasionally saw ``ConnectionRefusedError`` immediately
        after ``start()`` (most visibly in the parameterized datatype suite).
        """
        self._stop.clear()
        self._server.start()
        deadline = time.monotonic() + timeout
        connect_host = "127.0.0.1" if self.host in {"", "0.0.0.0", "::"} else self.host
        last_error: OSError | None = None
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((connect_host, self.port), timeout=0.1):
                    server_thread = getattr(self._server, "_thread", None)
                    if server_thread is None or server_thread.is_alive():
                        break
                    last_error = OSError("modbus_tk server thread exited before readiness")
            except OSError as exc:
                last_error = exc
            time.sleep(0.01)
        else:
            self._server.stop()
            raise OSError(
                f"Modbus mock server did not listen on {connect_host}:{self.port} "
                f"within {timeout:.1f}s: {last_error}"
            )
        self._updater = threading.Thread(target=self._update_loop, daemon=True)
        self._updater.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._server.stop()
        except Exception:  # noqa: BLE001
            pass
        updater = self._updater
        if updater is not None and updater is not threading.current_thread():
            updater.join(timeout=2.0)
            if updater.is_alive():
                raise RuntimeError("Modbus mock updater did not stop")
        self._updater = None

    # ------------------------------------------------------------------ writes
    def _slave(self, slave_id: Optional[int]):
        if slave_id is None:
            return self._slaves[0]
        return self._slave_by_id[slave_id]

    def write_registers(
        self, function_code: int, address: int, values: List[int], slave_id: Optional[int] = None
    ) -> None:
        """Raw write into the block matching ``function_code`` (1/2/3/4)."""
        fc = int(function_code)
        block_name = _BLOCK_NAME_BY_FC.get(fc)
        if block_name is None:
            raise ValueError(f"unsupported function_code {function_code!r}")
        self._slave(slave_id).set_values(block_name, address, list(values))

    def write_value(
        self,
        function_code: int,
        address: int,
        value,
        data_type: str,
        byte_order: str = "big",
        slave_id: Optional[int] = None,
    ) -> None:
        """Encode ``value`` as a real device would and write it at ``address``.

        For fc 1/2 (coils/discrete inputs) ``value`` is treated as a single
        bool bit regardless of ``data_type``. For fc 3/4 ``value`` is packed
        per :func:`encode_scalar`.
        """
        fc = int(function_code)
        if fc in (1, 2):
            self.write_registers(fc, address, [1 if value else 0], slave_id=slave_id)
            return
        self.write_registers(fc, address, encode_scalar(value, data_type, byte_order), slave_id=slave_id)

    # ------------------------------------------------------------------
    def _update_loop(self) -> None:
        import math

        counter = 0
        while not self._stop.wait(0.5):
            t = time.time()
            counter += 1
            for i, slave in enumerate(self._slaves):
                # 每个从站相位偏移一点,几台设备的曲线不完全重合。
                phase = i * 2.0
                temp = int(25 + 5 * math.sin((t + phase) / 10))
                humidity = int(50 + 10 * math.sin((t + phase) / 15))
                pressure = 1.0 + 0.5 * math.sin((t + phase) / 8)
                p_hi, p_lo = _f32_regs(pressure)
                c_hi, c_lo = _i32_regs(counter)
                try:
                    slave.set_values("holding", 0, [temp, humidity])
                    slave.set_values("holding", 10, [p_hi, p_lo])
                    slave.set_values("holding", 20, [c_hi, c_lo])
                except Exception:  # noqa: BLE001 - server stopped mid-write
                    return
