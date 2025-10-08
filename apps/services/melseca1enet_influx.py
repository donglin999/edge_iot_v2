#!/usr/bin/env python
# -*- encoding: utf-8 -*-
'''
@File    :   melseca1enet_influx.py
@Time    :   2024/03/12 
@Author  :   AI Assistant
@Version :   1.0
@Desc    :   三菱A1E系列PLC数据采集服务，将数据存储到InfluxDB
'''

# here put the import lib
import time
import threading
import queue

from apps.connect.connect_influx import InfluxClient, w_influx
from apps.connect.connect_melseca1enet_backu import MelsecA1ENetClient
from apps.utils.baseLogger import Log


class MelsecA1ENetInflux:
    def __init__(self, device_data_addresses):
        """
        初始化MelsecA1ENet数据采集服务
        
        Args:
            device_data_addresses: 设备数据地址配置
        """
        self.device_ip = device_data_addresses['source_ip']
        self.device_port = device_data_addresses['source_port']
        self.device_data_addresses = device_data_addresses
        self.device_fs = device_data_addresses['fs']
        # self.device_a_tag = "device_data_addresses['device_a_tag']"
        # self.device_name = device_data_addresses['device_name']
        
        # 如果配置中有PLC站号，则使用配置中的站号，否则默认为0
        self.plc_number = device_data_addresses.get('plc_number', 0)
        
        Log().printInfo(f"设备IP：{self.device_ip}：{self.device_port}，PLC站号：{self.plc_number}")

    def melseca1enet_influx(self):
        """
        主函数，启动数据采集和处理线程
        """
        # 创建MelsecA1ENet客户端
        melsec_client = MelsecA1ENetClient(
            self.device_ip, 
            self.device_port, 
            # self.device_a_tag,
            # self.device_name,
            self.plc_number
        )
        
        # 创建InfluxDB客户端
        influxdb_client = InfluxClient().connect()

        # 创建线程列表
        threads = []

        # 创建数据缓冲队列
        data_buff = queue.Queue(maxsize=100)
        
        # 添加数据采集线程
        threads.append(threading.Thread(
            target=self.get_data, 
            args=(data_buff, melsec_client, self.device_data_addresses)
        ))
        
        # 添加数据计算和存储线程
        threads.append(threading.Thread(
            target=self.calc_and_save, 
            args=(data_buff, influxdb_client)
        ))

        # 启动所有线程
        for thread in threads:
            thread.start()

    def get_data(self, data_buff, melsec_client, device_conf):
        """
        从PLC获取数据并放入缓冲队列
        
        Args:
            data_buff: 数据缓冲队列
            melsec_client: MelsecA1ENet客户端
            device_conf: 设备配置
        """
        print(f"配置为：{device_conf}")

        while True:
            try:
                # 读取PLC数据
                tag_data = melsec_client.read_plc(device_conf)
                print(tag_data)
                
                # 如果成功读取到数据
                if tag_data:
                    for i in tag_data:
                        # 添加时间戳
                        i['time'] = int(time.time_ns())
                        # 将数据放入缓冲队列
                        data_buff.put(i)

                # 按照配置的采集频率休眠
                time.sleep(self.device_fs)

            except Exception as e:
                Log().printError(f"从{self.device_ip}拿数据报错：{e}")
                print(f"从{self.device_ip}拿数据报错：{e}")
                continue

    def calc_and_save(self, data_buff, influxdb_client):
        """
        处理数据并保存到InfluxDB
        
        Args:
            data_buff: 数据缓冲队列
            influxdb_client: InfluxDB客户端
        """
        while True:
            try:
                # 从缓冲队列获取数据
                tag_data = data_buff.get()

                # 提取数据中的kafka_position和cn_name字段
                kafka_position = tag_data['kafka_position']
                cn_name = tag_data['cn_name']
                
                # 从数据中移除这些字段，因为它们不需要存储到InfluxDB
                tag_data.pop('kafka_position', None)
                tag_data.pop('cn_name', None)
                device_a_tag = tag_data['device_a_tag']
                device_name =  tag_data['device_name']

                # 将处理后的数据赋值给influx_data
                influx_data = tag_data

                # 检查数据是否为空
                if influx_data == {}:
                    Log().printError("melseca1enet data " + device_name + " is null")
                else:
                    # 将influx存储数据组织成对应的package存储格式
                    package = {
                        "measurement": device_a_tag,
                        "tags": {
                            "kafka_position": kafka_position,
                            "cn_name": cn_name
                        },
                        "fields": influx_data
                    }
                    json_body = [package]

                    # 写入InfluxDB
                    w_influx(influxdb_client, device_name, json_body)

            except Exception as e:
                Log().printError(f"{device_name}, calc_and_save error e: {e}")
                print(f"{device_name}, calc_and_save error e: {e}")
                # 如果发生异常，尝试重新连接InfluxDB
                influxdb_client = InfluxClient().connect() 