"""本地 OPC-UA mock 服务器 —— 无硬件联调用。

和 modbus_mock_server.py 同一个范式:这是**真的 OPC-UA 服务器**(python-opcua 的
``Server``),用它当被采对象,采集走的是真正的 ``OPCUAProtocol``(asyncua 客户端)。
所以像「NodeId 解析错误」「类型没对上」「一个坏点污染整批」这种只在真实 OPC-UA
会话/编解码路径才暴露的问题,用它才查得出来。

覆盖点:
  - 数据类型:Double / Float / Int16 / Int32 / Int64 / UInt16 / UInt32 / Boolean / String
  - NodeId 两种形态都有:``ns=X;i=数字`` 与 ``ns=X;s=字符串``
  - 数值随时间变化(正弦/单调递增/翻转/计数字符串),不是死值

注意:python-opcua(sync)只用来起服务器这一端;协议客户端侧用的是 asyncua,两者
是两套独立实现,谁也不依赖谁 —— 这正是我们想要的「真服务器 + 真协议客户端」组合。
"""
from __future__ import annotations

import math
import threading
import time
from typing import Dict, List

from opcua import Server, ua


class OPCUAMockServer:
    """在本地端口上跑一个 OPC-UA 服务器,持有一组随时间变化的节点。

    节点布局(命名空间 idx 由 ``register_namespace`` 分配,通常是 2):
        ns=2;i=1001   Double   d_double  —— 正弦, -50~50
        ns=2;i=1002   Float    d_float   —— 正弦, 0~100
        ns=2;s=Channel1.Device1.Int16Tag    Int16   d_int16  —— 三角波, -1000~1000
        ns=2;i=1004   Int32    d_int32   —— 单调递增
        ns=2;s=Channel1.Device1.Int64Tag    Int64   d_int64  —— 单调递增(大步长)
        ns=2;i=1006   UInt16   d_uint16  —— 正弦, 0~65535 区间内摆动
        ns=2;s=Channel1.Device1.UInt32Tag   UInt32  d_uint32 —— 单调递增
        ns=2;i=1008   Boolean  d_bool    —— 周期翻转
        ns=2;s=Channel1.Device1.StringTag   String  d_string —— 带计数的文本
    """

    #: 供采集配置/测试直接引用的测点定义。
    #: variant 是 ua.VariantType 名字,data_type 是协议侧 POINT_FIELDS.data_type 的值。
    POINTS: List[Dict] = [
        {"code": "d_double", "address": "ns=2;i=1001", "data_type": "double", "variant": "Double"},
        {"code": "d_float", "address": "ns=2;i=1002", "data_type": "float32", "variant": "Float"},
        {"code": "d_int16", "address": "ns=2;s=Channel1.Device1.Int16Tag", "data_type": "int16", "variant": "Int16"},
        {"code": "d_int32", "address": "ns=2;i=1004", "data_type": "int32", "variant": "Int32"},
        {"code": "d_int64", "address": "ns=2;s=Channel1.Device1.Int64Tag", "data_type": "auto", "variant": "Int64"},
        {"code": "d_uint16", "address": "ns=2;i=1006", "data_type": "uint16", "variant": "UInt16"},
        {"code": "d_uint32", "address": "ns=2;s=Channel1.Device1.UInt32Tag", "data_type": "uint32", "variant": "UInt32"},
        {"code": "d_bool", "address": "ns=2;i=1008", "data_type": "bool", "variant": "Boolean"},
        {"code": "d_string", "address": "ns=2;s=Channel1.Device1.StringTag", "data_type": "string", "variant": "String"},
    ]

    def __init__(self, host: str = "127.0.0.1", port: int = 15340) -> None:
        self.host = host
        self.port = port
        self.endpoint_url = f"opc.tcp://{host}:{port}/freeopcua/mock/"
        self._server: Server | None = None
        self._nodes: Dict[str, object] = {}
        self._idx: int | None = None
        self._stop = threading.Event()
        self._updater: threading.Thread | None = None

    # ------------------------------------------------------------------
    def start(self) -> None:
        server = Server()
        server.set_endpoint(self.endpoint_url)
        server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        idx = server.register_namespace("http://mock.edge-iot/opcua")
        self._idx = idx

        objects = server.get_objects_node()
        dev = objects.add_object(idx, "MockDevice")

        variant_map = {
            "Double": (ua.VariantType.Double, 0.0),
            "Float": (ua.VariantType.Float, 0.0),
            "Int16": (ua.VariantType.Int16, 0),
            "Int32": (ua.VariantType.Int32, 0),
            "Int64": (ua.VariantType.Int64, 0),
            "UInt16": (ua.VariantType.UInt16, 0),
            "UInt32": (ua.VariantType.UInt32, 0),
            "Boolean": (ua.VariantType.Boolean, False),
            "String": (ua.VariantType.String, ""),
        }
        for spec in self.POINTS:
            vtype, initial = variant_map[spec["variant"]]
            node = dev.add_variable(
                spec["address"], spec["code"], ua.Variant(initial, vtype),
            )
            node.set_writable()
            self._nodes[spec["code"]] = node

        server.start()
        self._server = server
        self._stop.clear()
        self._updater = threading.Thread(target=self._update_loop, daemon=True)
        self._updater.start()

    def stop(self) -> None:
        self._stop.set()
        if self._updater is not None:
            self._updater.join(timeout=2)
            self._updater = None
        if self._server is not None:
            try:
                self._server.stop()
            except Exception:  # noqa: BLE001
                pass
            self._server = None

    # ------------------------------------------------------------------
    def _update_loop(self) -> None:
        int32_counter = 0
        int64_counter = 0
        uint32_counter = 0
        toggle = False
        str_counter = 0

        while not self._stop.wait(0.3):
            t = time.time()
            int32_counter += 1
            int64_counter += 10_000_000_000
            uint32_counter = (uint32_counter + 1000) % 4_294_967_295
            toggle = not toggle
            str_counter += 1

            values = {
                "d_double": (math.sin(t / 5) * 50.0, ua.VariantType.Double),
                "d_float": (50.0 + math.sin(t / 7) * 50.0, ua.VariantType.Float),
                "d_int16": (int(math.sin(t / 3) * 1000), ua.VariantType.Int16),
                "d_int32": (int32_counter, ua.VariantType.Int32),
                "d_int64": (int64_counter, ua.VariantType.Int64),
                "d_uint16": (int(32768 + math.sin(t / 4) * 30000), ua.VariantType.UInt16),
                "d_uint32": (uint32_counter, ua.VariantType.UInt32),
                "d_bool": (toggle, ua.VariantType.Boolean),
                "d_string": (f"mock-{str_counter}", ua.VariantType.String),
            }
            try:
                for code, (value, vtype) in values.items():
                    self._nodes[code].set_value(ua.Variant(value, vtype))
            except Exception:  # noqa: BLE001 - server stopped mid-write
                return
