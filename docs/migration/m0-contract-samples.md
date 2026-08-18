# M0 脱敏契约样本

这些样本只描述基线形状，全部使用合成标识和值。它们不是完整的 M1 golden fixture，
也不得由生产导出替换。M1 应以真实 Django 黑盒响应生成机器可执行契约。

## REST

标准列表使用 limit/offset：

```json
{
  "count": 1,
  "next": null,
  "previous": null,
  "results": [
    {
      "id": 1,
      "code": "demo-site",
      "name": "演示站点",
      "description": "synthetic fixture",
      "created_at": "2026-08-17T08:00:00+08:00",
      "updated_at": "2026-08-17T08:00:00+08:00"
    }
  ]
}
```

启动采集成功响应并不是完整 Session serializer：

```json
{
  "detail": "任务启动成功",
  "session_id": 12,
  "celery_task_id": "synthetic-task-id",
  "task_code": "demo-task",
  "validation": {
    "all_healthy": true,
    "total_points": 2,
    "failed_points_count": 0,
    "device_results": {
      "demo-device": {
        "status": "healthy",
        "connected": true,
        "total_points": 2,
        "successful_points": 2,
        "failed_points": 0
      }
    }
  },
  "elapsed_seconds": 0.72
}
```

需要分别冻结的错误家族：

```json
{"detail": "未找到。"}
```

```json
{"sample_rate_hz": ["请确保该值大于或者等于 0.1。"]}
```

Excel 行错误也不是统一结构：SCADA/legacy 使用 `row`、`column`、`message`、
`protocol`；v2 使用 `row`、`sheet`、`column`、`message`、`kind`。上游 Influx 查询
失败的历史接口当前可能仍返回 HTTP 200 并增加 `error` 字段；M1 必须按现状测试，
不得在框架替换时顺手改变。

## WebSocket

Session 路径的数据帧：

```json
{
  "type": "data_point",
  "data": {
    "session_id": 12,
    "timestamp": "2026-08-17T00:00:00+00:00",
    "readings": [
      {
        "point_code": "demo-temperature",
        "value": 25.5,
        "quality": "good",
        "timestamp": "2026-08-17T00:00:00+00:00"
      }
    ],
    "batch_count": 1,
    "chunk_index": 0,
    "chunk_count": 1
  }
}
```

Global 路径对同一类数据使用 `type="data_point_update"`。告警又采用特殊信封：

```json
{
  "type": "alarm",
  "event": "created",
  "alarm": {"id": 99, "message": "synthetic alarm"}
}
```

当前帧没有 event id、sequence、游标或重放语义；Redis Channels 消息 10 秒过期。
因此断线后必须以 REST/Influx 重新同步，不能声称 exactly-once。

## InfluxDB

合成 line protocol 示例：

```text
demo-device,site=demo-site,device=demo-device,quality=good,cn_name=演示设备 demo-temperature=25.5,unit="°C" 1786924800000000000
session_health,session_id=12,task_code=demo-task,device_code=demo-device,device_ip=192.0.2.10,status=healthy consecutive_failures=0i,last_success_ts=1786924800.0,dropped_messages=0i,session_status="running" 1786924800000000000
```

地址使用文档保留网段 `192.0.2.0/24`，不对应现场设备。

## Excel

v2 每协议工作簿包含 `设备`、`测点`、`使用说明` 三个 sheet；前两个 sheet 第一行是
中文标签、第二行是英文机器键。SCADA 包含 `网关服务`、`设备与测点`、`使用说明`
三个 sheet；两个业务 sheet 只使用一行英文机器键表头，中文说明位于单元格 comment，
不能与 v2 的双行表头混用。上传字段名固定为 `file`，下载 MIME 为
`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`。

MQTT/OPC UA/SCADA 导出当前可能包含明文密码。契约 fixture 只能使用
`synthetic-password-not-a-secret` 等假值，严禁提交现场导出文件、token、IP 或账号。
