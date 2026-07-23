"""每个协议的**传输层**假替身。

和 ``tests/mocks/protocols.py`` 的区别很关键:那里是假的 *协议类*(整个
``BaseProtocol`` 子类都是编的),用来测流水线;这里假的是协议底下那一层
库(modbus_tk / paho / asyncua / snap7),**被测的是真正的协议实现** ——
真的 ``ModbusTCPProtocol.read_batch``、真的 ``MQTTProtocol._parse_message``、
真的地址解析和字节序解码。

只有这样,「连上设备 → 拿到数据」才算真的被验证过;否则测的只是我们自己写的
假协议返回了我们自己塞进去的值。

每个替身都能被切到「坏掉」模式(``fail=True``),用来驱动重连:连接抛异常 →
worker 落连接告警 → 切回正常 → 告警清除。
"""
from __future__ import annotations

import struct
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Modbus —— 假 modbus_tk master
# ---------------------------------------------------------------------------


class FakeModbusMaster:
    """替代 ``modbus_tk`` 的 TcpMaster / RtuMaster。

    ``execute`` 按请求的寄存器数量返回一段递增数据,足以让真实的解码逻辑
    (uint16 / int32 / float32 + 字节序)跑起来并产出确定值。
    """

    #: 类级开关:置 True 后新建的 master 一律连接失败(驱动重连用)。
    fail = False

    def __init__(self, *args, **kwargs) -> None:
        if FakeModbusMaster.fail:
            raise OSError("模拟:设备不可达")
        self.opened = False
        self.timeout = None
        self._sock = MagicMock()
        self.executed: List[Dict[str, Any]] = []

    def open(self) -> None:
        if FakeModbusMaster.fail:
            raise OSError("模拟:打开连接失败")
        self.opened = True

    def close(self) -> None:
        self.opened = False

    def set_timeout(self, value) -> None:
        self.timeout = value

    def execute(self, slave=None, function_code=None, starting_address=None,
                quantity_of_x=None, **kwargs):
        if FakeModbusMaster.fail:
            raise OSError("模拟:读取时链路已断")
        self.executed.append({
            "slave": slave,
            "function_code": function_code,
            "starting_address": starting_address,
            "quantity_of_x": quantity_of_x,
        })
        count = int(quantity_of_x or 1)
        # 从 1 开始的确定值,便于断言;首个寄存器恒为 1。
        return tuple(range(1, count + 1))


# ---------------------------------------------------------------------------
# MQTT / SCADA —— 假 paho client
# ---------------------------------------------------------------------------


class FakeMQTTClient:
    """替代 ``paho.mqtt.client.Client``。

    ``connect`` 时同步回调 ``on_connect``(真实 paho 是在网络线程里回调),
    让协议把话题订阅登记下来;随后把 :attr:`messages` 里预置的报文直接喂给
    ``on_message`` —— 走的是协议真正的解析路径。
    """

    fail = False
    #: [(topic, payload_bytes)],由用例按协议塞不同结构。
    messages: List[tuple] = []

    def __init__(self, *args, **kwargs) -> None:
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None
        self.subscriptions: List[str] = []
        self._connected = False
        self._loop_started = False

    # -- paho API --
    def username_pw_set(self, username, password) -> None:
        self.username, self.password = username, password

    def tls_set_context(self, context) -> None:
        self.tls_context = context

    def connect(self, host, port, keepalive=60) -> None:
        if FakeMQTTClient.fail:
            raise OSError("模拟:broker 不可达")
        self._connected = True
        if self.on_connect:
            self.on_connect(self, None, {}, 0)

    def subscribe(self, topic, qos=0) -> None:
        # 真 paho 在这里把 qos 打进报文;浮点 qos 正是在这一步炸掉的,
        # 所以替身也照做,免得放过同一个 bug。
        bytearray().append(qos)
        self.subscriptions.append(topic)

    def loop_start(self) -> None:
        self._loop_started = True
        # 订阅登记完之后再投递,顺序和真实 broker 一致。
        for topic, payload in FakeMQTTClient.messages:
            if self.on_message:
                self.on_message(self, None, _FakeMessage(topic, payload))

    def loop_stop(self) -> None:
        self._loop_started = False

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected


class _FakeMessage:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload
        self.qos = 0
        self.retain = False


# ---------------------------------------------------------------------------
# OPC-UA —— 假 asyncua Client
# ---------------------------------------------------------------------------


class FakeOPCUANode:
    def __init__(self, node_id: str, value: Any) -> None:
        self.nodeid = node_id
        self._value = value

    async def read_value(self):
        return self._value

    async def get_value(self):
        return self._value


class FakeOPCUAClient:
    """替代 ``asyncua.Client``,只实现协议真正用到的那几个 await 点。"""

    fail = False
    values: Dict[str, Any] = {}

    def __init__(self, url=None, timeout=None, **kwargs) -> None:
        self.url = url
        self.connected = False
        self.session_timeout = 0
        self.security_string = None

    async def set_security_string(self, value) -> None:
        self.security_string = value

    def set_user(self, user) -> None:
        self.user = user

    def set_password(self, password) -> None:
        self.password = password

    async def connect(self) -> None:
        if FakeOPCUAClient.fail:
            raise OSError("模拟:OPC-UA 服务端不可达")
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    def get_node(self, node_id):
        return FakeOPCUANode(node_id, FakeOPCUAClient.values.get(str(node_id), 0))

    async def read_values(self, nodes):
        return [await n.read_value() for n in nodes]


# ---------------------------------------------------------------------------
# Siemens S7 —— 假 snap7
# ---------------------------------------------------------------------------


class FakeS7Client:
    fail = False
    #: 按 (db, start) 给出的原始字节;缺省用 :attr:`default_payload`。
    areas: Dict[tuple, bytes] = {}
    default_payload = struct.pack(">f", 12.5)

    def __init__(self) -> None:
        self._connected = False

    def set_connection_type(self, value) -> None:
        self.connection_type = value

    def connect(self, ip, rack, slot, tcp_port: int = 102) -> None:
        if FakeS7Client.fail:
            raise OSError("模拟:PLC 不可达")
        self.tcp_port = tcp_port
        self._connected = True

    def get_connected(self) -> bool:
        return self._connected

    def disconnect(self) -> None:
        self._connected = False

    def read_area(self, area, db, start, size):
        if FakeS7Client.fail:
            raise OSError("模拟:读取时链路已断")
        payload = FakeS7Client.areas.get((db, start), FakeS7Client.default_payload)
        if len(payload) < size:
            payload = payload + b"\x00" * (size - len(payload))
        return bytearray(payload[:size])


class FakeArea:
    """站位用的 snap7 Area 枚举。协议只把它当不透明句柄传给 read_area。"""

    PE = "PE"
    PA = "PA"
    MK = "MK"
    DB = "DB"
    CT = "CT"
    TM = "TM"


def reset_all() -> None:
    """把所有替身恢复到「正常」状态 —— 用例之间必须互不影响。"""
    FakeModbusMaster.fail = False
    FakeMQTTClient.fail = False
    FakeMQTTClient.messages = []
    FakeOPCUAClient.fail = False
    FakeOPCUAClient.values = {}
    FakeS7Client.fail = False
    FakeS7Client.areas = {}
