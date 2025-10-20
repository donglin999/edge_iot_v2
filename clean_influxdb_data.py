#!/usr/bin/env python3
"""清理InfluxDB中的数据以避免字段类型冲突"""
from influxdb_client import InfluxDBClient
from influxdb_client.client.delete_api import DeleteApi
from datetime import datetime, timedelta

INFLUXDB_URL = "http://123.207.247.44:8086"
INFLUXDB_TOKEN = "ZoXRo0iE2d5rqvKPe_YNk3dus-xUPEcgPa5-e5LtUqB2OT6_yb5yyhJkes4J9bvf3FiWygVb6H8k3DSBIUwQaQ=="
INFLUXDB_ORG = "edge-iot"
INFLUXDB_BUCKET = "iot-data"

print("=" * 60)
print("清理 InfluxDB 数据")
print("=" * 60)

client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
delete_api = client.delete_api()

# 删除指定measurement的所有数据
measurements_to_delete = [
    "modbustcp-127.0.0.1-5020",
    "modbustcp-127.0.0.1-5021",
    "海天注塑机",
    "test_measurement",
]

# 时间范围：从1970年到现在+1天
start = datetime(1970, 1, 1)
stop = datetime.now() + timedelta(days=1)

for measurement in measurements_to_delete:
    print(f"\n删除 measurement: {measurement}")
    try:
        delete_api.delete(
            start=start,
            stop=stop,
            predicate=f'_measurement="{measurement}"',
            bucket=INFLUXDB_BUCKET,
            org=INFLUXDB_ORG
        )
        print(f"  ✓ 已删除 {measurement}")
    except Exception as e:
        print(f"  ✗ 删除失败: {e}")

client.close()

print("\n" + "=" * 60)
print("清理完成！")
print("现在可以重新启动采集任务了")
print("=" * 60)
