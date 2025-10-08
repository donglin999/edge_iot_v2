#!/usr/bin/env python
# -*- encoding: utf-8 -*-

import json
import threading
import time
from apps.connect.connect_mqtt import MqttClient
from lib.packages.write_influx import InfluxClient, Point
from settings import DevelopmentConfig
from apps.utils.baseLogger import Log

class MqttInflux:
    def __init__(self, data_addresses):
        self.data_addresses = data_addresses
        self.device_a_tag_dic = {}
        self.en_name_dic = {}
        self.cn_name_dic = {}
        self.source_addr = {}
        self.addr_type = {}
        if data_addresses['protocol_type'] == 'MQTT' or data_addresses['protocol_type'] == 'mqtt':
            self.device_a_tag_dic[data_addresses['device_a_tag']] = data_addresses['device_name']
            for k1, v1 in data_addresses.items():
                if isinstance(v1, dict):
                    print(f"v1:{v1}")
                    self.en_name_dic[v1['en_name']] = v1['cn_name']
                    self.source_addr[v1['source_addr']] = v1['en_name']
                    self.addr_type[v1['en_name']] = v1['type']
        else:
            for k, v in self.data_addresses.items():
                if isinstance(v, dict):
                    self.device_a_tag_dic[v['device_a_tag']] = v['device_name']
                    for k1, v1 in v.items():
                        if isinstance(v1, dict):
                            self.en_name_dic[v1['en_name']] = v1['cn_name']
                            self.source_addr[v1['source_addr']] = v1['en_name']
                            self.addr_type[v1['en_name']] = v1['type']

        print(f"self.source_addr:{self.source_addr}")
        print(f"self.addr_type:{self.addr_type}")
        print(f"self.device_a_tag_dic:{self.device_a_tag_dic}")
        print(f"self.en_name_dic:{self.en_name_dic}")
        self.old_data = {}
        self.points = []
        self.influx_client = InfluxClient(ip=DevelopmentConfig().INFLUXDB_HOST, port=DevelopmentConfig().INFLUXDB_PORT,
                                           my_token=DevelopmentConfig().INFLUXDB_TOKEN,
                                           my_org=DevelopmentConfig().INFLUXDB_ORG,
                                           my_bucket=DevelopmentConfig().INFLUXDB_BUCKET)

    def mqtt_influx(self):
        threads = [threading.Thread(target=self.mqtt_cpu, args=(self.data_addresses,)),
                   threading.Thread(target=self.cpu_influx, args=(self.old_data,))]

        # 数据计算和存储
        for thread in threads:
            thread.start()

    def mqtt_cpu(self, data_addresses):
        if DevelopmentConfig().MQTT_USERNAME:
            mqtt_client = MqttClient(data_addresses).connect()
        else:
            mqtt_client = MqttClient(data_addresses).connect2()
        # print(f"连接mqtt服务器完成")
        mqtt_client.on_message = self.get_data
        mqtt_client.loop_forever()

    def get_data(self, client, userdata, msg):
        try:
            msg_payload_dic = json.loads(str(msg.payload.decode("utf-8")))
            # print(f"收到的消息：{msg_payload_dic}")
            if 'data' in msg_payload_dic:
                if 'propertyCode' in msg_payload_dic['data']:
                    propertyCode = msg_payload_dic['data']['propertyCode']
                    device_a_tag = msg_payload_dic['data']['deviceCode']
                else:
                    propertyCode = "None"
            else:
                propertyCode = "None"
            if propertyCode in self.source_addr:
                # print(f"msg_payload_dic:{msg_payload_dic}")
                try:
                    value = float(msg_payload_dic['data']['propertyValue'])
                except Exception as e:
                    if msg_payload_dic['data']['propertyValue'] == True:
                        value = 1
                    elif msg_payload_dic['data']['propertyValue'] == False:
                        value = 0
                    else:
                        value = str(msg_payload_dic['data']['propertyValue'])
                if device_a_tag in self.old_data:
                    pass
                else:
                    self.old_data[device_a_tag] = {}
                self.old_data[device_a_tag][self.source_addr[propertyCode]] = value
        except Exception as e:
            Log().printError(f"get_data报错：{e}，数据为：{msg_payload_dic}")
            print(f"get_data报错：{e}，数据为：{msg_payload_dic}")

    def cpu_influx(self, old_data):
        while True:
            # print(f"self.old_data：{self.old_data}")
            for k, v in old_data.items():
                for field, value in v.items():
                    point = Point(k).time(time.time_ns())
                    point = point.tag("device_a_tag", k)
                    point = point.tag("device_name", self.device_a_tag_dic[k])
                    point = point.tag("data_source", "MQTT")
                    point = point.tag("kafka_position", "measures")
                    # print(f"field：{field}")
                    point = point.tag("cn_name", self.en_name_dic[field])

                    point = point.tag("location", "nansha")
                    point = point.tag("factory", "bingxiang")
                    point = point.tag("device", "injection")
                    point = point.tag("machine_id", self.device_a_tag_dic[k] + '-' + k)

                    # print(f"添加标签成功，cn_name: {self.en_name_dic[field]}")
                    point = point.field(field, value)
                    # print(f"数据库行数据创建成功point:{point}")
                    self.points.append(point)
            # print(f"len(points)：{len(points)}")
            if len(self.points) > 10:
                # 创建一个线程来处理数据
                thread_args = {"data": self.points}
                thread = threading.Thread(target=self.influx_client.write_client, kwargs=thread_args)
                # 启动线程
                thread.start()
                self.points = []
                # print(f"数据写入数据库成功,self.old_data{self.old_data}")
            time.sleep(1)


