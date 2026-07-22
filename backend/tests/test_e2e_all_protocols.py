"""全协议全流程拉通 —— 导入 → 连接 → 取数 → 入库 → 断线重连。

每个已注册协议都跑同一条链路,**用的是真正的协议实现**,只把它底下的传输库
(modbus_tk / paho / asyncua / snap7)换成 ``tests/mocks/transports.py`` 里的
替身。这一点是刻意的:如果连协议类本身都是假的,那测到的只是「我们编的假协议
返回了我们自己塞进去的值」,证明不了「设备连得上、数据取得到」。

五段,和现场关心的顺序一致:

1. **导入** —— 按协议自己的 FieldSpec 生成一行 Excel,走真实导入器落库。
2. **连接** —— 真协议 + 假传输,connect 成功。
3. **取数** —— 走真实的解码路径(Modbus 字节序、MQTT payload 解析、S7 地址
   解析),拿到确定值。
4. **入库** —— 经 ``ReadWorker`` 把读数交到 sink 层。
5. **重连** —— 传输层切「坏」→ 连接告警落库、设备状态变离线;切回正常 →
   告警清除、状态回在线。这条同时锁住「永远重试、没有第三态」的设计。

全部同步跑在测试线程里(``worker.run()``),不起真线程、不连 Redis/InfluxDB。
"""
from __future__ import annotations

import json
import struct
import threading
from contextlib import contextmanager
from types import ModuleType
from typing import Any, Dict

import pandas as pd
import pytest

from acquisition import models as acq_models
from acquisition.protocols import ProtocolRegistry
from acquisition.protocols import modbus as modbus_mod
from acquisition.protocols import mqtt as mqtt_mod
from acquisition.protocols import opcua as opcua_mod
from acquisition.protocols import s7 as s7_mod
from acquisition.services.acquisition_service import AcquisitionService
from acquisition.services.device_config import build_device_config
from acquisition.services.device_status import (
    STATUS_OFFLINE,
    STATUS_ONLINE,
    compute_device_statuses,
)
from acquisition.services.pipeline import ReadWorker
from acquisition.services.sinks import Sink
from configuration import models
from configuration.services.importer import ExcelImportService
from tests.mocks import transports
from tests.mocks.transports import (
    FakeArea,
    FakeMQTTClient,
    FakeModbusMaster,
    FakeOPCUAClient,
    FakeS7Client,
)
from tests.fixtures.factories import *  # noqa: F401,F403

pytestmark = pytest.mark.e2e


# ===========================================================================
# 每个协议的一份「剧本」
# ===========================================================================


PRODUCT_KEY = "123daffb91264286adcdf3bfe55194c7"
SCADA_DEVICE = "A0201010001150403"


def _scada_topic(code: str) -> str:
    return f"/sys/{PRODUCT_KEY}/device/{SCADA_DEVICE}/thing/property/{code}/post"


#: protocol -> 剧本
#:   row       导入用的 Excel 行(只写这个协议真正需要的列)
#:   expect    读到的第一个测点的期望值
#:   messages  仅 MQTT 系:预置的报文
SCRIPTS: Dict[str, Dict[str, Any]] = {
    "modbus_tcp": {
        "row": {
            "protocol_type": "modbus_tcp",
            "device_name": "Modbus TCP 机台",
            "source_ip": "192.168.10.11",
            "source_port": 502,
            "slave_id": 1,
            "code": "temp_01",
            "address": "40001",
            "function_code": 3,
            "data_type": "uint16",
            "num": 1,
        },
        # FakeModbusMaster 返回 (1, 2, 3, ...) —— 首个寄存器恒为 1
        "expect": 1,
    },
    "modbus_rtu": {
        "row": {
            "protocol_type": "modbus_rtu",
            "device_name": "Modbus RTU 机台",
            "serial_port": "/dev/ttyUSB0",
            "baudrate": 9600,
            "parity": "N",
            "bytesize": 8,
            "stopbits": 1,
            "slave_id": 1,
            "code": "temp_01",
            "address": "40001",
            "function_code": 3,
            "data_type": "uint16",
            "num": 1,
        },
        "expect": 1,
    },
    "mqtt": {
        "row": {
            "protocol_type": "mqtt",
            "device_name": "MQTT 传感器",
            "source_ip": "broker.local",
            "source_port": 1883,
            "mqtt_topics": "sensor/line1/temp",
            "mqtt_qos": 0,
            "code": "temperature",
            "data_type": "float",
        },
        "messages": [("sensor/line1/temp", json.dumps({"temperature": 26.5}).encode())],
        "expect": 26.5,
    },
    "scada": {
        "row": {
            "protocol_type": "scada",
            "device_name": "中山注塑机",
            "source_ip": "10.134.14.147",
            "source_port": 8883,
            "mqtt_username": "ZYY_XJDZS",
            "mqtt_password": "secret",
            "mqtt_use_tls": True,
            "mqtt_qos": 0,
            "scada_product_key": PRODUCT_KEY,
            "scada_device_name": SCADA_DEVICE,
            "scada_topic_template":
                "/sys/{product_key}/device/{device_name}/thing/property/{code}/post",
            "code": "N270400150027",
            "description": "注射压力实际值",
            "data_type": "float",
        },
        # 真实网关的负载结构:值在 data.propertyValue(6e15018 修的就是这个)
        "messages": [(
            _scada_topic("N270400150027"),
            json.dumps({
                "data": {
                    "deviceCode": SCADA_DEVICE,
                    "propertyCode": "N270400150027",
                    "dataType": 2,
                    "propertyValue": "88.5",
                    "time": "1755653755532",
                }
            }).encode(),
        )],
        "expect": 88.5,
    },
    "opcua": {
        "row": {
            "protocol_type": "opcua",
            "device_name": "OPC-UA 服务器",
            "endpoint_url": "opc.tcp://192.168.10.50:4840",
            "security_policy": "None",
            "code": "motor_speed",
            "address": "ns=2;s=Channel1.Device1.Tag1",
            "data_type": "auto",
        },
        "opcua_values": {"ns=2;s=Channel1.Device1.Tag1": 1450.0},
        "expect": 1450.0,
    },
    "siemens_s7": {
        "row": {
            "protocol_type": "siemens_s7",
            "device_name": "S7-1200 PLC",
            "source_ip": "192.168.10.20",
            "source_port": 102,
            "rack": 0,
            "slot": 1,
            "plc_type": "S7-1200",
            "code": "motor_speed",
            "address": "DB1.DBD0",
            "data_type": "float32",
        },
        # FakeS7Client 默认返回 big-endian float 12.5
        "expect": 12.5,
    },
}

ALL_PROTOCOLS = sorted(SCRIPTS)


# ===========================================================================
# 传输层替身的装卸
# ===========================================================================


@pytest.fixture(autouse=True)
def _clean_transports():
    transports.reset_all()
    yield
    transports.reset_all()


@contextmanager
def fake_transport(protocol: str, script: Dict[str, Any], monkeypatch):
    """把该协议底下的库换成替身;协议实现本身原样使用。"""
    if protocol == "modbus_tcp":
        monkeypatch.setattr(modbus_mod.modbus_tcp, "TcpMaster", FakeModbusMaster)
    elif protocol == "modbus_rtu":
        monkeypatch.setattr(modbus_mod.modbus_rtu, "RtuMaster",
                            lambda ser: FakeModbusMaster())
        # 串口设备在 CI 上不存在,serial.Serial 也得挡掉。
        import serial
        monkeypatch.setattr(serial, "Serial", lambda **kwargs: object())
    elif protocol in ("mqtt", "scada"):
        FakeMQTTClient.messages = script.get("messages", [])
        monkeypatch.setattr(mqtt_mod.mqtt, "Client", FakeMQTTClient)
    elif protocol == "opcua":
        FakeOPCUAClient.values = script.get("opcua_values", {})
        monkeypatch.setattr(opcua_mod, "AsyncUAClient", FakeOPCUAClient)
        monkeypatch.setattr(opcua_mod, "_OPCUA_AVAILABLE", True)
    elif protocol == "siemens_s7":
        fake_snap7 = type("snap7", (), {"client": type("c", (), {"Client": FakeS7Client})})
        monkeypatch.setattr(s7_mod, "snap7", fake_snap7)
        monkeypatch.setattr(s7_mod, "Area", FakeArea)
        monkeypatch.setattr(s7_mod, "_SNAP7_AVAILABLE", True)
    else:  # pragma: no cover - 新协议加进 SCRIPTS 时会被下面的覆盖测试抓到
        raise AssertionError(f"没有为 {protocol} 准备传输层替身")
    yield


def break_transport(protocol: str) -> None:
    """把该协议的传输层切到「设备不可达」。"""
    if protocol.startswith("modbus"):
        FakeModbusMaster.fail = True
    elif protocol in ("mqtt", "scada"):
        FakeMQTTClient.fail = True
    elif protocol == "opcua":
        FakeOPCUAClient.fail = True
    elif protocol == "siemens_s7":
        FakeS7Client.fail = True


def heal_transport(protocol: str) -> None:
    if protocol.startswith("modbus"):
        FakeModbusMaster.fail = False
    elif protocol in ("mqtt", "scada"):
        FakeMQTTClient.fail = False
    elif protocol == "opcua":
        FakeOPCUAClient.fail = False
    elif protocol == "siemens_s7":
        FakeS7Client.fail = False


# ===========================================================================
# 工具
# ===========================================================================


class CaptureSink(Sink):
    """记下每一条落到 sink 层的读数 —— 站位 InfluxDB。"""

    def __init__(self) -> None:
        self.readings = []

    def consume(self, reading) -> None:
        self.readings.append(reading)


def import_one_protocol(tmp_path, protocol: str, script: Dict[str, Any]):
    """走真实导入器把该协议的一行 Excel 落库,返回 (device, points)。"""
    path = tmp_path / f"{protocol}.xlsx"
    pd.DataFrame([script["row"]]).to_excel(path, index=False, sheet_name="采集点配置")

    job = models.ImportJob.objects.create(source_name=path.name)
    service = ExcelImportService(job, path)
    summary = service.run_validation()
    assert not summary.row_errors, [e.to_dict() for e in summary.row_errors]
    service.apply(site_code=f"e2e-{protocol}", created_by="e2e", mode="merge")

    device = models.Device.objects.get(protocol=protocol, site__code=f"e2e-{protocol}")
    return device, list(device.points.all())


def make_worker(device, task, session, sink, **kwargs) -> ReadWorker:
    """用真实的分组逻辑造一个 ReadWorker(读数走真协议 + 假传输)。

    走 ``AcquisitionService`` 而不是手搓 point dict —— 分组/读计划本身也是
    链路的一环,手搓就把它绕过去了。
    """
    service = AcquisitionService(task, session)
    group = service.device_groups[device.id]
    return ReadWorker(
        device=group["device"],
        points=group["points"],
        sinks=[sink],
        sample_rate_hz=float(task.sample_rate_hz),
        shutdown_event=threading.Event(),
        health_dict={},
        session=session,
        **kwargs,
    )


# ===========================================================================
# 1) 导入
# ===========================================================================


@pytest.mark.django_db
@pytest.mark.parametrize("protocol", ALL_PROTOCOLS)
def test_import_creates_device_and_points(tmp_path, protocol):
    device, points = import_one_protocol(tmp_path, protocol, SCRIPTS[protocol])

    assert device.protocol == protocol
    assert len(points) == 1
    assert points[0].code == SCRIPTS[protocol]["row"]["code"]
    # 编码里不许有 pandas 的 .0 残留(见 test_excel_numeric_fidelity)
    assert ".0" not in device.code

    # 导进来的配置足以构造协议对象,必填项一个不缺
    config = build_device_config(device)
    klass = ProtocolRegistry.get(protocol)
    missing = [
        f.name for f in klass.DEVICE_FIELDS
        if f.required and config.get(f.name) in (None, "")
    ]
    assert not missing, f"{protocol} 缺必填字段: {missing}"


# ===========================================================================
# 2) + 3) 连接 & 取数(真协议 + 假传输)
# ===========================================================================


@pytest.mark.django_db
@pytest.mark.parametrize("protocol", ALL_PROTOCOLS)
def test_connect_and_read_real_value(tmp_path, protocol, monkeypatch):
    script = SCRIPTS[protocol]
    device, points = import_one_protocol(tmp_path, protocol, script)

    with fake_transport(protocol, script, monkeypatch):
        proto = ProtocolRegistry.create(protocol, build_device_config(device))
        assert proto.connect() is not False
        assert proto.is_connected

        point_dicts = [{
            "code": p.code,
            "address": p.address,
            "data_type": (p.extra or {}).get("data_type", "uint16"),
            "num": (p.extra or {}).get("num", 1),
            "function_code": (p.extra or {}).get("function_code"),
            **(p.extra or {}),
        } for p in points]

        readings = proto.read_points(point_dicts)
        assert readings, f"{protocol} 一条数据都没读到"

        first = readings[0]
        assert first["quality"] == "good", first
        assert float(first["value"]) == pytest.approx(script["expect"]), (
            f"{protocol} 读到的值不对: {first}"
        )
        proto.disconnect()


# ===========================================================================
# 4) 入库 —— 读数经 ReadWorker 落到 sink
# ===========================================================================


@pytest.mark.django_db
@pytest.mark.parametrize("protocol", ALL_PROTOCOLS)
def test_readings_reach_the_sink(tmp_path, protocol, monkeypatch, create_task, create_session):
    script = SCRIPTS[protocol]
    device, points = import_one_protocol(tmp_path, protocol, script)
    task = create_task(points=points)
    session = create_session(task=task)
    sink = CaptureSink()

    with fake_transport(protocol, script, monkeypatch):
        worker = make_worker(device, task, session, sink)
        worker._connect()
        assert worker.protocol is not None and worker.protocol.is_connected
        worker._read_cycle()

    assert sink.readings, f"{protocol} 的读数没进 sink"
    reading = sink.readings[0]
    assert reading.point_code == points[0].code
    assert reading.quality == "good", f"{protocol} 读到坏质量: {reading}"
    assert float(reading.value) == pytest.approx(script["expect"]), (
        f"{protocol} 进 sink 的值不对: {reading}"
    )


# ===========================================================================
# 5) 断线 → 告警 + 离线 → 恢复 → 清除 + 在线
# ===========================================================================


@pytest.mark.django_db
@pytest.mark.parametrize("protocol", ALL_PROTOCOLS)
def test_offline_alarm_then_recovery(tmp_path, protocol, monkeypatch, create_task, create_session):
    script = SCRIPTS[protocol]
    device, points = import_one_protocol(tmp_path, protocol, script)
    # 设备要处在「采集中」才谈得上在线/离线
    task = create_task(points=points)
    session = create_session(task=task)
    sink = CaptureSink()

    dedup = f"connectivity:{device.code}"

    def firing():
        return acq_models.Alarm.objects.filter(
            category="connectivity", dedup_key=dedup,
            status=acq_models.Alarm.STATUS_FIRING,
        ).exists()

    with fake_transport(protocol, script, monkeypatch):
        worker = make_worker(
            device, task, session, sink,
            max_reconnect=1, reconnect_backoff=0.01,
        )

        # --- 设备挂了 ---
        break_transport(protocol)
        for _ in range(3):
            worker._connect()
        worker._raise_offline_alarm()

        assert firing(), f"{protocol} 掉线后没有落连接告警"
        assert compute_device_statuses([device])[device.id] == STATUS_OFFLINE

        # --- 设备回来了:重连必须能成功,不能因为之前失败过就放弃 ---
        heal_transport(protocol)
        worker._connect()
        assert worker.protocol.is_connected, f"{protocol} 恢复后没能重连上"

        worker._read_cycle()
        assert sink.readings, f"{protocol} 恢复后没能继续取数"

    device.refresh_from_db()
    assert not firing(), f"{protocol} 恢复后连接告警没清除"
    assert compute_device_statuses([device])[device.id] == STATUS_ONLINE


# ===========================================================================
# 覆盖度看门狗:新增协议必须补进这份剧本
# ===========================================================================


def test_every_user_facing_protocol_has_a_script():
    """加了新协议却没加剧本,这条会红 —— 免得「全协议跑通」名不副实。

    比对的是 ``describe_all()``,也就是 ``/api/acquisition/protocols/`` 真正
    给前端的那份清单 —— 而不是全局注册表:注册表会被别的用例塞进 mock 协议,
    也含 ``plc``/``mc`` 这种没在 ``protocols/__init__.py`` 里 import、
    因而对用户根本不存在的条目。
    """
    import acquisition.protocols as proto_pkg

    # 生产协议 = ``protocols/__init__.py`` 真正 import 进来的那些模块里定义的类。
    # 不能直接读全局注册表:别的用例会往里塞 mock 协议,而且注册表里还有
    # ``plc``/``mc`` 这种没被 __init__ import、对用户根本不存在的条目。
    prod_modules = {
        m.__name__ for m in vars(proto_pkg).values()
        if isinstance(m, ModuleType) and m.__name__.startswith("acquisition.protocols.")
    } - {"acquisition.protocols.base"}

    exposed = {
        name for name, klass in ProtocolRegistry._protocols.items()
        # name == META.name 过滤掉别名(modbustcp / s7 / opc-ua ...)
        if klass.__module__ in prod_modules and name == klass.META.name
    }
    assert exposed == set(SCRIPTS), (
        f"缺剧本: {exposed - set(SCRIPTS)};多余剧本: {set(SCRIPTS) - exposed}"
    )
