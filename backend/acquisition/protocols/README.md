# 协议适配开发指南

本目录下的每个 `*.py` 文件实现一个工业通信协议。整套架构是**声明式**的——
新协议的字段约束由代码声明,导入校验、动态表单、Excel 模板生成、API
schema 全部自动派生,**不需要修改任何核心代码**。

## 加一个新协议:四步

```python
# protocols/my_protocol.py
from typing import Any, Dict, List
from .base import (
    BaseProtocol,
    ConnectionError,
    FieldSpec,
    ProtocolMeta,
    ProtocolRegistry,
    ReadError,
)


@ProtocolRegistry.register("my_protocol", "myprot")  # 多个别名以逗号分隔
class MyProtocol(BaseProtocol):
    # ---- 1. 声明 ProtocolMeta ----
    META = ProtocolMeta(
        name="my_protocol",
        label="我的协议",
        category="industrial-ethernet",      # fieldbus | industrial-ethernet | iot | opc
        description="一句话说明这个协议是干啥的",
        supports_pause=True,
    )

    # ---- 2. 声明 DEVICE_FIELDS / POINT_FIELDS / IDENTITY_FIELDS ----
    DEVICE_FIELDS = (
        FieldSpec("source_ip", "PLC IP", required=True, example="192.168.1.10"),
        FieldSpec("source_port", "端口", kind="int", default=502),
        FieldSpec("timeout", "超时(秒)", kind="float", default=5.0),
    )
    POINT_FIELDS = (
        FieldSpec("code", "测点编码", required=True, example="motor_speed"),
        FieldSpec("address", "地址", required=True),
        FieldSpec("data_type", "数据类型", kind="enum",
                  choices=("int16", "int32", "float32"), default="float32"),
    )
    # device 唯一性:同一(协议, 这些字段值)算同一台设备,Excel 多行可合并
    IDENTITY_FIELDS = ("source_ip", "source_port")

    # ---- 3. 实现 4 个生命周期方法 ----
    def __init__(self, device_config: Dict[str, Any]) -> None:
        super().__init__(device_config)
        self.ip = device_config["source_ip"]
        self.port = int(device_config.get("source_port", 502))

    def connect(self) -> bool:
        # 建立长连接;失败 raise ConnectionError
        ...
        self.is_connected = True
        return True

    def disconnect(self) -> None: ...

    def read_points(self, points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        # 接收 POINT_FIELDS 经 coerce 后的字典列表,返回:
        # [{"code": "...", "value": ..., "timestamp": <ns>, "quality": "good"}]
        ...

    def health_check(self) -> bool: ...
```

## 4. 注册 import

```python
# protocols/__init__.py
from . import my_protocol  # noqa: F401
```

只要这一步生效,运行时:

* `/api/acquisition/protocols/` 会出现 `my_protocol` 条目
* `/api/acquisition/protocols/my_protocol/` 返回完整 schema
* `/api/acquisition/protocols/template/?protocols=my_protocol` 下载专用 Excel 模板
* Excel 导入时,`protocol_type=my_protocol` 的行用此 schema 校验
* 设备页"添加设备"下拉自动出现"我的协议"选项,点选后表单按 `DEVICE_FIELDS` 渲染
* 现有 Pause/Resume/告警 等功能都自动适用,无需为新协议改任何 view 代码

## FieldSpec 字段说明

| 字段        | 含义                                                                                      |
| ----------- | ----------------------------------------------------------------------------------------- |
| `name`      | 机器键。Excel 列名 / `device.metadata` 字典 key / `point.extra` 字典 key 都用它           |
| `label`     | 中文显示名,在表单/Excel 注释中可见                                                       |
| `kind`      | 控件类型:`string` / `int` / `float` / `bool` / `enum` / `secret`                         |
| `required`  | True 时,Excel 行缺该列直接报行级错误                                                     |
| `default`   | 缺省值。`required=True` 时仍允许有 default(用于必填但有合理默认的场景)                  |
| `choices`   | `kind=enum` 必填,值列表                                                                  |
| `help_text` | 在表单提示气泡 / Excel 表头注释中显示                                                     |
| `example`   | 在生成 Excel 模板时填进示例行                                                             |

## 现成的工具:`_combine_registers`

如果协议是 16-bit 字模型(Modbus、S7 中的 W/D 寄存器),可以参考
`modbus.py` 的 `_combine_registers(registers, data_type, num, byte_order)`:
按 `data_type=float32/int32/double/int64/...` 把多个 16-bit 寄存器组合成
对应的 Python 标量。直接复用或抄到自己的协议里。

## 故障排除

* **连不上**:协议构造期不要做 IO,留到 `connect()` 里。`__init__` 只读 dict。
* **必填字段没生效**:确认 `FieldSpec(required=True)` 且 **没填 default** —— 有 default 就不会再被认为缺失。
* **设备一个变成多个**:`IDENTITY_FIELDS` 选错。这是设备级唯一键,不要把可变字段放进去(比如 timeout 不该是 identity)。
* **加完协议没出现在 /api/acquisition/protocols/**:漏写 `protocols/__init__.py` 的 import。
