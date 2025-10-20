#!/usr/bin/env python3
"""测试远程InfluxDB写入"""
import time
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

# 远程InfluxDB配置
INFLUXDB_URL = "http://123.207.247.44:8086"
INFLUXDB_TOKEN = "ZoXRo0iE2d5rqvKPe_YNk3dus-xUPEcgPa5-e5LtUqB2OT6_yb5yyhJkes4J9bvf3FiWygVb6H8k3DSBIUwQaQ=="
INFLUXDB_ORG = "edge-iot"
INFLUXDB_BUCKET = "iot-data"

def test_connection():
    """测试连接"""
    print(f"连接到 {INFLUXDB_URL}...")
    try:
        client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
        health = client.health()
        print(f"✓ InfluxDB健康状态: {health.status}")
        print(f"  版本: {health.version}")
        return client
    except Exception as e:
        print(f"✗ 连接失败: {e}")
        return None

def test_write(client):
    """测试写入数据"""
    print(f"\n写入测试数据到 bucket '{INFLUXDB_BUCKET}'...")
    try:
        write_api = client.write_api(write_options=SYNCHRONOUS)

        # 创建测试数据点
        point = Point("test_measurement") \
            .tag("site", "test_site") \
            .tag("device", "test_device") \
            .tag("point", "test_point") \
            .field("value", 123.45) \
            .time(int(time.time() * 1e9))

        write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
        print("✓ 数据写入成功!")

        # 写入采集设备的模拟数据
        print("\n写入模拟采集数据...")
        for i in range(5):
            point = Point("海天注塑机") \
                .tag("site", "factory_01") \
                .tag("device", "modbustcp-127.0.0.1-5020") \
                .tag("point", f"温度_{i+1}") \
                .tag("quality", "good") \
                .field(f"温度_{i+1}", 25.0 + i * 0.5) \
                .time(int((time.time() + i) * 1e9))

            write_api.write(bucket=INFLUXDB_BUCKET, org=INFLUXDB_ORG, record=point)
            print(f"  ✓ 写入测点 温度_{i+1}: {25.0 + i * 0.5}")

        print("\n✓ 所有测试数据写入成功!")
        return True
    except Exception as e:
        print(f"✗ 写入失败: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_query(client):
    """测试查询数据"""
    print(f"\n查询最近写入的数据...")
    try:
        query_api = client.query_api()

        query = f'''
        from(bucket:"{INFLUXDB_BUCKET}")
          |> range(start: -1h)
          |> filter(fn: (r) => r["_measurement"] == "test_measurement" or r["_measurement"] == "海天注塑机")
          |> limit(n: 10)
        '''

        result = query_api.query(query, org=INFLUXDB_ORG)

        if not result:
            print("  未查询到数据")
            return False

        print("✓ 查询结果:")
        count = 0
        for table in result:
            for record in table.records:
                count += 1
                print(f"  {count}. {record.get_measurement()}:{record.get_field()} = {record.get_value()} @ {record.get_time()}")

        print(f"\n✓ 共查询到 {count} 条记录")
        return True
    except Exception as e:
        print(f"✗ 查询失败: {e}")
        import traceback
        traceback.print_exc()
        return False

def main():
    print("="*60)
    print("InfluxDB 远程连接测试")
    print("="*60)

    # 测试连接
    client = test_connection()
    if not client:
        print("\n测试失败: 无法连接到InfluxDB")
        return

    # 测试写入
    if not test_write(client):
        print("\n测试失败: 数据写入失败")
        return

    # 测试查询
    if not test_query(client):
        print("\n警告: 数据查询失败，但写入可能成功")

    client.close()
    print("\n" + "="*60)
    print("测试完成!")
    print("="*60)
    print("\n请在InfluxDB UI中查看数据:")
    print(f"  URL: {INFLUXDB_URL}")
    print(f"  Bucket: {INFLUXDB_BUCKET}")
    print(f"  Measurement: test_measurement, 海天注塑机")

if __name__ == '__main__':
    main()
