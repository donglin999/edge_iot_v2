#!/usr/bin/env python
# -*- encoding: utf-8 -*-

# here put the import lib
from influxdb_client import InfluxDBClient
from influxdb_client.client.write_api import SYNCHRONOUS

from settings import DevelopmentConfig
from apps.utils.baseLogger import Log


def w_influx(client, device_name, data):
    write_api = client.write_api(write_options=SYNCHRONOUS)
    write_api.write(DevelopmentConfig().INFLUXDB_BUCKET, DevelopmentConfig().INFLUXDB_ORG, data)
    # Log().printInfo("device: " + device_name + "; data already store influxdb!")


class InfluxClient:
    def __init__(self):
        self.org = DevelopmentConfig().INFLUXDB_ORG
        self.ip = DevelopmentConfig().INFLUXDB_HOST
        self.port = DevelopmentConfig().INFLUXDB_PORT
        self.token = DevelopmentConfig().INFLUXDB_TOKEN
        # print(f"InfluxDB 配置: 组织={self.org}, IP={self.ip}, 端口={self.port}, 令牌={self.token}")

    def connect(self):
        try:
            client = InfluxDBClient(url="http://" + self.ip + ":" + str(self.port),
                                    token=self.token, org=self.org)
            # Log().printInfo(f"创建influx客户端完成")
            return client

        except Exception as e:
            Log().printError(f"创建influx客户端报错：{e}")
            print(f"创建influx客户端报错：{e}")
            raise

    def get_count(self, client, measurement, field, begin_time, end_time):
        # 从influxdb中获取指定measurement的唯一valuecount值
        try:
            query_api = client.query_api()
            # 构建查询语句，查询指定时间范围内的数据
            query = f'''from(bucket:"{DevelopmentConfig().INFLUXDB_BUCKET}")
                |> range(start: {begin_time}, stop: {end_time})
                |> filter(fn: (r) => r["_measurement"] == "{measurement}")
                |> filter(fn: (r) => r["_field"] == "{field}")
                |> count(column: "_value")
                |> yield(name: "count")'''

            # 执行查询
            result = query_api.query(query=query, org=DevelopmentConfig().INFLUXDB_ORG)

            # 处理查询结果
            count = 0
            for table in result:
                for record in table.records:
                    count = record.get_value()
                    break  # 只需要第一条记录的值

            return count
        except Exception as e:
            Log().printError(f"获取数据计数失败：{e}")
            return 0


if __name__ == '__main__':
    influx_client = InfluxClient()
    json_body = []
    json = {
        "measurement": 'test',
        "machine_id": 'A0201010001150330',
        "tags": {
            "tag1": "t1"
        },
        "fields": {
            "LefA": 1
        }
    }
    json_body.append(json)
    w_influx(influx_client, 'A0201010001150330', json_body)