"""Excel 导入的数值保真 —— pandas 把整数读成 float64 引发的两处真实故障。

pandas 读 Excel 时,任何整数单元格都会变成 float64。这一个事实同时打穿了两条路:

1. **枚举字段变浮点,采集在现场才炸。** ``mqtt_qos`` 以 ``0.0`` 进到配置里,
   而 ``0.0 in (0, 1, 2)`` 为真,所以校验、coerce、连接全都一路绿灯 ——
   直到真的连上 broker、开始组 SUBSCRIBE 包时才抛
   ``'float' object cannot be interpreted as an integer``。也就是说
   「导入成功 + 测试连接通过」之后,采集依然起不来。

2. **设备编码带上 ``.0``,导入和界面各建一台。** 端口 502 变成 ``502.0``,
   于是导入得到 ``modbus_tcp-192.168.1.100-502.0-1.0``,界面新建同一台设备
   得到 ``modbus_tcp-192.168.1.100-502-1``,两个 code 不相等 ——
   本该更新的设备被又建了一台。

两处都不是协议各自的问题,所以修在共用的 ``_coerce_enum`` 和 ``_device_code``,
新增协议自动受益。
"""
from __future__ import annotations

import pandas as pd
import pytest

from acquisition.protocols import ProtocolRegistry
from acquisition.protocols.base import _coerce_enum
from configuration import models
from configuration.services.importer import ExcelImportService, _device_code
from tests.fixtures.factories import *  # noqa: F401,F403


# ===========================================================================
# 1) 枚举 coerce 必须还原成 choices 声明的类型
# ===========================================================================


@pytest.mark.parametrize(
    "raw, choices, expected",
    [
        (0.0, (0, 1, 2), 0),          # pandas 读出来的 qos
        (2.0, (0, 1, 2), 2),
        ("1", (0, 1, 2), 1),          # 手填的字符串
        (8.0, (7, 8), 8),             # 串口数据位
        (1.0, (1, 2), 1),             # 停止位
        ("big", ("big", "little"), "big"),
    ],
)
def test_coerce_enum_returns_choice_type(raw, choices, expected):
    value = _coerce_enum(raw, choices)
    assert value == expected
    # 关键在类型:0.0 == 0 为真,但 bytearray.append(0.0) 会 TypeError
    assert type(value) is type(expected)


def test_coerce_enum_float_qos_is_packable():
    """qos 必须能直接进 MQTT 报文 —— 这正是现场炸掉的那一步。"""
    qos = _coerce_enum(0.0, (0, 1, 2))
    bytearray().append(qos)  # 浮点会在这里 TypeError


def test_coerce_enum_unmatched_still_falls_through():
    """匹配不上的值仍原样交给校验器报错,不许在这里吞掉。"""
    assert _coerce_enum(9.0, (0, 1, 2)) == "9.0"


@pytest.mark.parametrize("protocol", ["mqtt", "scada"])
def test_coerce_device_normalises_pandas_floats(protocol):
    """走协议自己的 coerce_device,浮点 qos 必须落成 int。"""
    klass = ProtocolRegistry.get(protocol)
    coerced = klass.coerce_device({"mqtt_qos": 0.0, "source_ip": "10.0.0.1", "source_port": 1883.0})
    assert coerced["mqtt_qos"] == 0
    assert type(coerced["mqtt_qos"]) is int
    assert type(coerced["source_port"]) is int


# ===========================================================================
# 2) 设备编码不许出现 .0
# ===========================================================================


def test_device_code_renders_integral_floats_as_integers():
    """pandas 的 502.0 必须和界面路径的 502 生成同一个 code。"""
    assert _device_code("modbus_tcp", ("192.168.1.100", 502.0, 1.0)) == (
        "modbus_tcp-192.168.1.100-502-1"
    )


def test_device_code_keeps_real_decimals():
    """真的有小数的值不能被抹掉 —— 只归一化整数值的浮点。"""
    assert _device_code("demo", (1.5,)) == "demo-1.5"


# ===========================================================================
# 3) 端到端:一份 pandas 风格的表导进来,设备编码与配置都干净
# ===========================================================================


@pytest.fixture
def float_workbook(tmp_path):
    """模拟 pandas 读 Excel 的结果:所有整数列都是 float64。"""
    path = tmp_path / "floats.xlsx"
    pd.DataFrame(
        {
            "protocol_type": ["mqtt", "modbus_tcp"],
            "device_name": ["MQTT 设备", "Modbus 设备"],
            "source_ip": ["broker.example.com", "192.168.1.100"],
            "source_port": [1883.0, 502.0],
            "slave_id": [None, 1.0],
            "mqtt_topics": ["sensor/+/temp", None],
            "mqtt_qos": [0.0, None],
            "code": ["temperature", "temp_01"],
            "data_type": ["float", "uint16"],
            "address": [None, "40001"],
            "function_code": [None, 3.0],
        }
    ).to_excel(path, index=False, sheet_name="采集点配置")
    return path


@pytest.mark.django_db
def test_import_produces_clean_codes_and_int_enums(float_workbook):
    job = models.ImportJob.objects.create(source_name="floats.xlsx")
    service = ExcelImportService(job, float_workbook)

    summary = service.run_validation()
    assert not summary.row_errors, [e.to_dict() for e in summary.row_errors]

    service.apply(site_code="numfid", created_by="test", mode="merge")

    mqtt_device = models.Device.objects.get(protocol="mqtt", site__code="numfid")
    modbus_device = models.Device.objects.get(protocol="modbus_tcp", site__code="numfid")

    # 编码里不许有 .0,且与界面路径一致
    assert ".0" not in mqtt_device.code
    assert modbus_device.code == "modbus_tcp-192.168.1.100-502-1"

    # 落库的 metadata 已经是干净类型
    assert type(mqtt_device.metadata["mqtt_qos"]) is int
    assert type(mqtt_device.metadata["source_port"]) is int


@pytest.mark.django_db
def test_imported_mqtt_device_config_is_ready_for_paho(float_workbook):
    """导入的设备走完整条采集链路取配置,qos 必须能直接进报文。"""
    from acquisition.services.device_config import build_device_config

    job = models.ImportJob.objects.create(source_name="floats.xlsx")
    service = ExcelImportService(job, float_workbook)
    service.run_validation()
    service.apply(site_code="numfid2", created_by="test", mode="merge")

    device = models.Device.objects.get(protocol="mqtt", site__code="numfid2")
    config = build_device_config(device)
    protocol = ProtocolRegistry.create("mqtt", config)

    qos = getattr(protocol, "device_config", config).get("mqtt_qos", config.get("mqtt_qos"))
    bytearray().append(qos)  # 浮点在这里 TypeError —— 就是现场那一炸
