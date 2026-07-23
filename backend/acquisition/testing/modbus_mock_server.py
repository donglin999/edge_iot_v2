"""本地 Modbus TCP mock 服务器 —— 无硬件联调用。

和 simulator 协议不同,这个是**真的 Modbus TCP 服务器**(modbus_tk 的 TcpServer)。
用它当被采对象,采集走的是真正的 ModbusTCPProtocol:真开 TCP、真发功能码、真读
寄存器、真做字节序/多寄存器解码。所以像「float32 被当 uint16 读半个值」这种只在
modbus 解码路径才暴露的 bug,用它才查得出 —— simulator 永远返回 good,查不出来。

保持字节序一致:协议侧默认 big-endian(ABCD)。float32/int32 占 2 个保持寄存器,
按大端拆成高低字写进保持寄存器区,协议读回来解码应还原成原值。
"""
from __future__ import annotations

import struct
import threading
import time
from typing import Dict, List, Tuple

import modbus_tk.defines as cst
from modbus_tk import modbus_tcp


def _f32_regs(value: float) -> Tuple[int, int]:
    """float32 → (高位寄存器, 低位寄存器),大端。"""
    hi, lo = struct.unpack(">HH", struct.pack(">f", value))
    return hi, lo


def _i32_regs(value: int) -> Tuple[int, int]:
    hi, lo = struct.unpack(">HH", struct.pack(">i", value))
    return hi, lo


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
    ) -> None:
        self.host = host
        self.port = port
        self.slave_ids = slave_ids or [1]
        self._server = modbus_tcp.TcpServer(address=host, port=port)
        self._slaves = []
        for sid in self.slave_ids:
            slave = self._server.add_slave(sid)
            # 保持寄存器区,覆盖到 int32 计数所在的 22 个字。
            slave.add_block("holding", cst.HOLDING_REGISTERS, 0, 32)
            self._slaves.append(slave)
        self._stop = threading.Event()
        self._updater: threading.Thread | None = None

    # ------------------------------------------------------------------
    def start(self) -> None:
        self._server.start()
        self._updater = threading.Thread(target=self._update_loop, daemon=True)
        self._updater.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._server.stop()
        except Exception:  # noqa: BLE001
            pass

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
