"""本地 Siemens S7 mock 服务器 —— 无硬件联调用。

和 modbus_mock_server.py / opcua_mock_server.py 同一个范式:这是**真的 S7
服务器**(python-snap7 的 ``snap7.server.Server``,底层是官方 snap7 C 库),
用它当被采对象,采集走的是真正的 ``SiemensS7Protocol``(真 ``snap7.client.Client``
+ 真 ISO-on-TCP 握手 + 真 ``Srv_ReadArea``)。所以像"地址解析对不上""字节序错""
一个坏点污染整批"这种只在真实 S7 通信路径才暴露的问题,用它才查得出来。

真实 S7 服务器固定监听 102 端口需要 root/CAP_NET_BIND_SERVICE(``Srv_Start`` 内部
调 bind(0.0.0.0:102)),开发机上没有 root 通常起不来。好在 snap7 服务器支持
``Srv_SetParam(p_u16_LocalPort, ...)`` 换端口(``Server.start(tcp_port=...)``
就是这个),换成 1024 以上的端口就不需要 root —— 所以本文件默认换端口起,
不做 102 降级。

覆盖区域(与 ``acquisition/protocols/s7.py`` 的地址解析一一对应):
    DB<db_number>   register_area(SrvArea.DB, db_number, buf)   DBX/DBB/DBW/DBD
    M               register_area(SrvArea.MK, 0, buf)           M/MX/MB/MW/MD
    I               register_area(SrvArea.PE, 0, buf)           I/IX/IB/IW/ID
    Q               register_area(SrvArea.PA, 0, buf)           Q/QX/QB/QW/QD

每块区域都是一段可写的 ctypes 缓冲区,``write_bytes`` / ``set_bit`` 直接改这段
内存,服务器线程读到的就是新值 —— 不需要重启服务器。
"""
from __future__ import annotations

import ctypes
import logging
import socket
import struct
import threading
import time
from typing import Dict, Optional

try:
    import snap7
    from snap7.server import Server as _Snap7Server
    from snap7.type import SrvArea
    _SNAP7_AVAILABLE = True
except ImportError:  # pragma: no cover - graceful fallback, see s7.py
    _Snap7Server = None
    SrvArea = None
    _SNAP7_AVAILABLE = False

logger = logging.getLogger(__name__)


class S7MockServer:
    """在本地端口上跑一个真 S7 服务器,持有 DB/M/I/Q 四块可写内存。

    默认布局按 ``acquisition/protocols/s7.py`` 里文档化的地址形式铺开,方便
    测试直接引用:

        DB1.DBX0.0 .. DBX0.7   bool     （DB 区第 0 字节的 8 个位）
        DB1.DBB1               byte
        DB1.DBW2               int16/uint16
        DB1.DBD4               int32/uint32/float32 (4 字节)
        DB1.DBD8               float64/lreal (8 字节,占 DBD8..DBD15)
        DB1.DBB16 起           string（Siemens 格式:maxlen,len,chars...)
        M0.0 / MW10 / MD20     merker 区同构
        I0.0 / IW10            输入区（只读语义,这里照样允许写,方便测试布点）
        Q0.0 / QW10            输出区
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 15402,
        db_number: int = 1,
        db_size: int = 256,
        mk_size: int = 64,
        pe_size: int = 64,
        pa_size: int = 64,
        port_retries: int = 10,
    ) -> None:
        if not _SNAP7_AVAILABLE:
            raise RuntimeError("python-snap7 未安装,无法起真 S7 mock 服务器")
        self.host = host
        self.requested_port = port
        self.port: Optional[int] = None
        self.db_number = db_number
        self.port_retries = max(1, port_retries)

        self._server = _Snap7Server(log=False)
        # 每块区域一段独立的 ctypes 缓冲区；snap7 服务器直接把它当共享内存读写。
        self._db_buf = (ctypes.c_ubyte * db_size)()
        self._mk_buf = (ctypes.c_ubyte * mk_size)()
        self._pe_buf = (ctypes.c_ubyte * pe_size)()
        self._pa_buf = (ctypes.c_ubyte * pa_size)()
        self._server.register_area(SrvArea.DB, db_number, self._db_buf)
        self._server.register_area(SrvArea.MK, 0, self._mk_buf)
        self._server.register_area(SrvArea.PE, 0, self._pe_buf)
        self._server.register_area(SrvArea.PA, 0, self._pa_buf)

        self._area_bufs: Dict[str, "ctypes.Array"] = {
            "DB": self._db_buf,
            "M": self._mk_buf,
            "I": self._pe_buf,
            "Q": self._pa_buf,
        }
        self._started = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> int:
        """启动服务器,端口被占用时在 ``port_retries`` 次内顺延重试。

        返回实际监听的端口。找不到可用端口时抛 ``RuntimeError``。
        """
        last_exc: Optional[Exception] = None
        for attempt in range(self.port_retries):
            candidate = self.requested_port + attempt
            if not self._port_free(candidate):
                continue
            try:
                self._server.start(tcp_port=candidate)
            except Exception as exc:  # noqa: BLE001 - snap7 raises RuntimeError w/ bytes msg
                last_exc = exc
                continue
            self.port = candidate
            self._started = True
            return candidate
        raise RuntimeError(
            f"S7 mock 服务器起不来(尝试端口 {self.requested_port}.."
            f"{self.requested_port + self.port_retries - 1}): {last_exc}"
        )

    def stop(self) -> None:
        if not self._started:
            return
        try:
            self._server.stop()
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                self._server.destroy()
            except Exception:  # noqa: BLE001
                pass
            self._started = False

    def __enter__(self) -> "S7MockServer":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    @staticmethod
    def _port_free(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            return s.connect_ex(("127.0.0.1", port)) != 0

    # ------------------------------------------------------------------
    # 写内存 —— 测试用来预置已知字节
    # ------------------------------------------------------------------
    def write_bytes(self, area: str, offset: int, data: bytes) -> None:
        """``area`` 是 'DB'/'M'/'I'/'Q'（DB 目前只支持单个 db_number）。"""
        buf = self._area_bufs[area.upper()]
        for i, b in enumerate(data):
            buf[offset + i] = b

    def set_bit(self, area: str, byte_offset: int, bit: int, value: bool) -> None:
        buf = self._area_bufs[area.upper()]
        if value:
            buf[byte_offset] |= (1 << bit)
        else:
            buf[byte_offset] &= ~(1 << bit) & 0xFF

    def write_float32(self, area: str, offset: int, value: float) -> None:
        self.write_bytes(area, offset, struct.pack(">f", value))

    def write_float64(self, area: str, offset: int, value: float) -> None:
        self.write_bytes(area, offset, struct.pack(">d", value))

    def write_int16(self, area: str, offset: int, value: int) -> None:
        self.write_bytes(area, offset, struct.pack(">h", value))

    def write_uint16(self, area: str, offset: int, value: int) -> None:
        self.write_bytes(area, offset, struct.pack(">H", value))

    def write_int32(self, area: str, offset: int, value: int) -> None:
        self.write_bytes(area, offset, struct.pack(">i", value))

    def write_uint32(self, area: str, offset: int, value: int) -> None:
        self.write_bytes(area, offset, struct.pack(">I", value))

    def write_string(self, area: str, offset: int, text: str, max_len: int = 32) -> None:
        """Siemens S7 STRING: byte0=maxlen, byte1=actual len, bytes 2..=chars。"""
        raw = text.encode("utf-8")[:max_len]
        header = bytes([max_len, len(raw)])
        self.write_bytes(area, offset, header + raw)
