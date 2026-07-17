# 中山小家电 注塑机 SCADA 网关 采集配置示例

协议：`scada`（基于 MQTT 的 SCADA 网关采集协议，见
`backend/acquisition/protocols/scada.py`）。

网关每个测点发布一个 MQTT 话题，遵循阿里云 IoT 风格模式：

```
/sys/{product_key}/device/{device_name}/thing/property/{code}/post
```

采集端只订阅一次，用单层通配符 `+` 替换 `{code}`，再从每条消息的话题反解出
测点编码 `{code}`，映射回对应测点后写入 InfluxDB。

## 设备连接（DEVICE_FIELDS）

| 字段 | 值 | 说明 |
| --- | --- | --- |
| `source_ip` | `10.134.14.147` | Broker 地址 |
| `source_port` | `8883` | MQTT-over-TLS 端口 |
| `mqtt_use_tls` | `true` | 8883 默认开启 TLS |
| `mqtt_username` | `ZYY_XJDZS` | 用户名 |
| `mqtt_password` | `<在前端配置时填写>` | 密码，运行时在前端填写，切勿写入代码/模板 |
| `mqtt_qos` | `0` | QoS |
| `scada_product_key` | `123daffb91264286adcdf3bfe55194c7` | 话题中的 `{product_key}` 段 |
| `scada_device_name` | `A0201010001150403` | 话题中的 `{device_name}` 段 |
| `scada_topic_template` | `/sys/{product_key}/device/{device_name}/thing/property/{code}/post` | 话题模板 |

订阅话题（自动生成）：

```
/sys/123daffb91264286adcdf3bfe55194c7/device/A0201010001150403/thing/property/+/post
```

## 测点（POINT_FIELDS）

| `code` | `description`（中文名称） | `data_type` | `unit` |
| --- | --- | --- | --- |
| `N270400150027` | 注射压力实际值 | `float` | |
| `N270400151293` | 注射速度实际值 | `float` | |
| `N270400151294` | 注射位置实际值 | `float` | |
| `F40040100030008` | 射胶时间实际值 | `float` | |

`payload_path` 留空即可：采集端会自动走网关真实结构 `data.propertyValue`，并保留
旧结构兜底（见下）。

## JSON 配置（可直接喂给导入/建站接口）

```json
{
  "protocol": "scada",
  "device": {
    "source_ip": "10.134.14.147",
    "source_port": 8883,
    "mqtt_use_tls": true,
    "mqtt_username": "ZYY_XJDZS",
    "mqtt_password": "<在前端配置时填写>",
    "mqtt_qos": 0,
    "scada_product_key": "123daffb91264286adcdf3bfe55194c7",
    "scada_device_name": "A0201010001150403",
    "scada_topic_template": "/sys/{product_key}/device/{device_name}/thing/property/{code}/post"
  },
  "points": [
    {"code": "N270400150027", "description": "注射压力实际值", "data_type": "float"},
    {"code": "N270400151293", "description": "注射速度实际值", "data_type": "float"},
    {"code": "N270400151294", "description": "注射位置实际值", "data_type": "float"},
    {"code": "F40040100030008", "description": "射胶时间实际值", "data_type": "float"}
  ]
}
```

## 消息负载解析

### 真实结构（已用真机网关验证，即当前契约）

```json
{"data": {
    "deviceCode":    "AN300400152600059",
    "propertyCode":  "N180100560001",
    "dataType":      2,
    "propertyValue": "2",
    "time":          "1755653755532"
}}
```

| 字段 | 说明 |
| --- | --- |
| `data.propertyValue` | **数值，字符串形式**（`"2"`）。按测点 `data_type` 做类型转换 |
| `data.propertyCode` | **权威测点编码**，优先于话题反解出的 `{code}` |
| `data.deviceCode` | 网关自身设备号；仅供参考（协议实例已知道自己在读哪台设备） |
| `data.time` | **毫秒字符串**，归一到纳秒（见下） |
| `data.dataType` | int 枚举，含义未知，**不依赖**；类型转换仍以测点 `data_type` 为准 |

个别网关会把 `data` 双重编码成 JSON 字符串，采集端会自动 `json.loads` 兼容。

### 取值顺序（容错）

`payload_path` 非空时直接按点号路径取值（优先级最高，跳过自动探测）；留空时：

1. `payload["data"]["propertyValue"]` ← **真实结构，主路径**
2. payload 本身是标量（数字/字符串/布尔）  ← 以下均为旧结构兜底
3. `payload["value"]`
4. `payload[code]`
5. `payload["params"][code]["value"]`，否则 `payload["params"][code]`

### 时间戳归一

优先取 `data.time`，否则取 payload 顶层的 `time` / `timestamp` / `ts`；`str`/`int`
均可。按**数量级**判定单位并统一换算成纳秒：

| 数量级 | 单位 | 换算 |
| --- | --- | --- |
| `< 1e11` | 秒 | `×1e9` |
| `< 1e14` | 毫秒 ← 网关实际用这个 | `×1e6` |
| `< 1e17` | 微秒 | `×1e3` |
| 其他 | 纳秒 | 原样 |

例：`"1755653755532"` → `1755653755532000000`（2025-08-20）。缺失 / 非法（含 `<=0`）
时回落到消息接收时间（`MQTTProtocol` 已用 `time.time_ns()` 打点，本身即纳秒，不再换算）。

非 JSON / 畸形负载会记录日志并跳过，不会中断采集循环。
