#!/usr/bin/env python
# -*- encoding: utf-8 -*-
'''
@File    :   collector_kafka.py
@Time    :   2023/05/11 15:11:40
@Author  :   Jason Jiangfeng 
@Version :   1.0
@Contact :   jiangfeng24@midea.com
@Desc    :   获取kafka数据
'''

# here put the import lib
import time
import threading
import queue

from apps.connect.connect_influx import InfluxClient, w_influx
from apps.connect.connect_plc import PLCClient
from apps.utils.baseLogger import Log


class PlcInflux:
    def __init__(self, device_data_addresses):
        self.device_ip = device_data_addresses['source_ip']
        self.device_port = device_data_addresses['source_port']
        self.device_data_addresses = device_data_addresses
        self.device_fs = device_data_addresses['fs']
        # self.device_a_tag = device_data_addresses['device_a_tag']
        # self.device_name = device_data_addresses['device_name']
        Log().printInfo(f"设备IP：{self.device_ip}：{self.device_port}")

    def monitor_queue_length(self, data_buff):
        """监控队列长度"""
        while True:
            current_size = data_buff.qsize()
            Log().printInfo(f"设备 {self.device_ip} 当前队列长度: {current_size}")
            time.sleep(5)  # 每5秒检查一次

    def plc_influx(self):
        """
        主函数
        """
        plc_client = PLCClient(self.device_ip, self.device_port)
        # plc_client = PLCClient(self.device_ip, self.device_port, self.device_name)
        # mqtt_client = MqttClient().connect(self.device_name)
        influxdb_client = InfluxClient().connect()

        threads = []

        data_buff = queue.Queue(maxsize=1000)
        threads.append(threading.Thread(target=self.get_data, args=(data_buff, plc_client, self.device_data_addresses)))
        # 数据计算和存储
        threads.append(threading.Thread(target=self.calc_and_save, args=(data_buff, influxdb_client)))
        # threads.append(threading.Thread(target=self.monitor_queue_length, args=(data_buff,)))

        for thread in threads:
            thread.start()

    def get_data(self, data_buff, plc_client, device_conf):
        #print(f"配置为：{device_conf}")

        while True:
            try:
                tag_data = plc_client.read_plc(device_conf)

                #Log().printInfo(f"从{self.device_ip}plc读到的数据tag_data：{tag_data}")
                if tag_data:
                    for i in tag_data:
                        i['time'] = int(time.time_ns())

                        data_buff.put(i)

                # time.sleep(self.device_fs)

            except Exception as e:
                Log().printError(f"从{self.device_ip}拿数据报错：{e}")
                print(f"从{self.device_ip}拿数据报错：{e}")
                continue

    def calc_and_save(self, data_buff, influxdb_client):
        # 创建一个列表来存储积累的数据
        batch_data = []
        batch_size = 50  # 设置批量提交的数据量

        while True:
            try:
                tag_data = data_buff.get()

                kafka_position = tag_data['kafka_position']
                cn_name = tag_data['cn_name']
                device_a_tag = tag_data['device_a_tag']
                device_name =  tag_data['device_name']
                tag_data.pop('kafka_position', None)
                tag_data.pop('cn_name', None)

                influx_data = tag_data

                if influx_data == {}:
                    Log().printError("plc data " + self.device_ip + " is null")
                else:
                    # 将influx存储数据组织成对应的package存储格式
                    package = \
                        {
                            "measurement": device_a_tag,
                            "tags":
                                {
                                    "kafka_position": kafka_position,
                                    "cn_name": cn_name
                                },
                            "fields": influx_data
                        }
                    # 将数据添加到批处理列表中
                    batch_data.append(package)
                    # if device_a_tag == "OilSealingBefore_P":
                    #     print(f"数据为：{package}")

                    # 当积累的数据达到指定数量时，一次性提交
                    if len(batch_data) >= batch_size:
                        w_influx(influxdb_client, device_name, batch_data)
                        # Log().printInfo(f"批量提交了 {len(batch_data)} 条数据到InfluxDB:{batch_data}")
                        # 清空列表，开始新一轮积累
                        batch_data = []

            except Exception as e:
                Log().printError(f"{self.device_ip}, calc_and_save error e: {e}")
                print(f"{self.device_ip}, calc_and_save error e: {e},data:{tag_data}")
                influxdb_client = InfluxClient().connect()

                # 如果发生错误但已有积累的数据，尝试提交这些数据
                if batch_data:
                    try:
                        w_influx(influxdb_client, self.device_ip, batch_data)
                        Log().printInfo(f"错误恢复后提交了 {len(batch_data)} 条数据到InfluxDB")
                        batch_data = []
                    except Exception as e2:
                        Log().printError(f"{self.device_ip}, 尝试提交积累数据时出错: {e2}")

            # mqtt数据发送的topic组织成："工厂/设备类型/A码/设备名/opc"的格式
            # json_data = json.dumps(mqtt_data)
            # pub_mqtt(mqtt_client,
            #         getconfig('factory') + '/' +
            #         getconfig('device') + '/' +
            #         self.device_a_tag + '/' +
            #         self.device_name + '/kafka', json_data)

