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

`payload_path` 留空即可，采集端会自动探测常见 payload 结构（见下）。

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

## 消息负载解析（容错顺序）

对每条话题匹配成功、且测点被订阅的消息，按以下顺序提取数值（`payload_path`
非空时直接按点号路径取值，跳过自动探测）：

1. payload 本身是标量（数字/字符串/布尔）
2. `payload["value"]`
3. `payload[code]`
4. `payload["params"][code]["value"]`，否则 `payload["params"][code]`

时间戳优先取 payload 里的 `time` / `timestamp` / `ts`，否则用消息接收时间
（`time.time_ns()`）。非 JSON / 畸形负载会记录日志并跳过，不会中断采集循环。

> 待确认：真实 payload 的确切结构与时间戳单位（秒 / 毫秒 / 纳秒）。拿到一条真实
> 样本后即可收紧 `payload_path` 与时间戳换算，去掉自动探测的兜底分支。
