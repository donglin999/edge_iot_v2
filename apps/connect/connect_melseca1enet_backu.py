#!/usr/bin/env python
# -*- encoding: utf-8 -*-
'''
@File    :   connect_melseca1enet.py
@Time    :   2024/03/12 
@Author  :   AI Assistant
@Version :   1.0
@Desc    :   三菱A1E系列PLC连接模块
'''

import json
import re
import time
import struct
from kafka import KafkaProducer
from lib.HslCommunication import MelsecA1ENet
from apps.connect.connect_influx import w_influx, InfluxClient
from settings import DevelopmentConfig
from apps.utils.baseLogger import Log

def circular_shift_left(value, shift):
    bit_size = 32  # 32位整数
    # 计算实际需要移动的位数，因为移动32位相当于没有移动
    shift = shift % bit_size
    # 进行循环左移
    return ((value << shift) | (value >> (bit_size - shift))) & ((1 << bit_size) - 1)

class MelsecA1ENetClient:
    def __init__(self, ip, port, plc_number=0) -> None:
        """
        初始化MelsecA1ENet客户端
        
        Args:
            ip: PLC的IP地址
            port: PLC的端口号
            device_a_tag: 设备A码
            device_name: 设备名称
            plc_number: PLC站号，默认为0
        """
        self.ip = ip
        self.port = port
        # self.device_a_tag = device_a_tag
        # self.device_name = device_name
        self.plc_number = plc_number
        
        # 初始化Kafka连接（如果启用）
        if DevelopmentConfig().KAFKA_ENABLED:
            self.kafka_producer = KafkaProducer(
                bootstrap_servers=DevelopmentConfig().kafka_bootstrap_servers,
                value_serializer=lambda x: json.dumps(x).encode('utf-8')
            )
            print(f"kafka连接成功，{DevelopmentConfig().kafka_bootstrap_servers}")
            Log().printInfo(f"kafka连接成功，{DevelopmentConfig().kafka_bootstrap_servers}")
            
        # 初始化InfluxDB连接
        self.influxdb_client = InfluxClient().connect()
        Log().printInfo(f"influxdb对象创建成功")
        
        # 初始化MelsecA1ENet连接
        self.plc = MelsecA1ENet(self.ip, self.port)
        self.plc.ConnectServer()
        Log().printInfo(f"MelsecA1ENet连接成功，IP: {self.ip}, 端口: {self.port}")
        print(f"MelsecA1ENet连接成功，IP: {self.ip}, 端口: {self.port}")
        self.count = 0
        # print(f"尝试读数据")
        # result = self.plc.ReadInt16("D5000", 1).Content[0]
        # print(f"result:{result}")

    def contains_alpha_or_digit(self, s):
        """检查字符串是否包含字母或数字"""
        # 检查是否包含字母
        contains_alpha = any(char.isalpha() for char in s)
        # 检查是否包含数字
        contains_digit = any(char.isdigit() for char in s)

        # 如果包含字母或数字，就打印消息
        if contains_alpha or contains_digit:
            print(f"{s}变量包含字母或数字")
        return contains_alpha or contains_digit

    def read_plc(self, register_dict):
        """
        读取PLC数据
        
        Args:
            register_dict: 寄存器配置字典
            
        Returns:
            list: 读取到的数据列表
        """
        try:
            tag_data = []
            for en_name, register_conf in register_dict.items():
                if isinstance(register_conf, dict):
                    try:
                        num = int(register_conf['num'])
                        addr = register_conf['source_addr']
                        coefficient = register_conf['coefficient']
                        kafka_position = register_conf['kafka_position']
                        precision = int(register_conf['precision'])
                        cn_name = register_conf['cn_name']
                        device_a_tag = register_conf['device_a_tag']
                        device_name = register_conf['device_name']
                        plc_data = {}
                        
                        # 根据不同的数据类型读取数据
                        if register_conf['type'] == 'str':
                            if en_name == "codeResult":
                                read_data = str(self.plc.ReadString(addr, num).Content)[4:64]
                                # 检查是否包含字母或数字
                                if self.contains_alpha_or_digit(read_data):
                                    pass
                                else:
                                    read_data = "None"
                                plc_data[en_name] = re.sub(r'\s+', '', read_data)
                                plc_data['kafka_position'] = kafka_position
                                plc_data['cn_name'] = cn_name
                                plc_data['device_a_tag'] = device_a_tag
                                plc_data['device_name'] = device_name

                            else:
                                plc_data[en_name] = str(self.plc.ReadString(addr, num).Content)
                                plc_data['kafka_position'] = kafka_position
                                plc_data['cn_name'] = cn_name
                                plc_data['device_a_tag'] = device_a_tag
                                plc_data['device_name'] = device_name

                        elif register_conf['type'] == 'int16':
                            plc_data[en_name] = round(self.plc.ReadInt16(addr, num).Content[0] * coefficient, precision)
                            plc_data['kafka_position'] = kafka_position
                            plc_data['cn_name'] = cn_name
                            plc_data['device_a_tag'] = device_a_tag
                            plc_data['device_name'] = device_name

                        elif register_conf['type'] == 'int32':
                            plc_data[en_name] = round(self.plc.ReadInt32(addr, num).Content[0] * coefficient, precision)
                            plc_data['kafka_position'] = kafka_position
                            plc_data['cn_name'] = cn_name
                            plc_data['device_a_tag'] = device_a_tag
                            plc_data['device_name'] = device_name

                        elif register_conf['type'] == 'float':
                            plc_data[en_name] = round(self.plc.ReadFloat(addr, num).Content[0] * coefficient, precision)
                            plc_data['kafka_position'] = kafka_position
                            plc_data['cn_name'] = cn_name
                            plc_data['device_a_tag'] = device_a_tag
                            plc_data['device_name'] = device_name

                        elif register_conf['type'] == 'float2':
                            value = self.plc.ReadUInt32(addr, num).Content[0]
                            value2 = circular_shift_left(value, 16)
                            value3 = struct.unpack('<f', struct.pack('<I', value2))[0]
                            plc_data[en_name] = round(value3, precision)
                            plc_data['kafka_position'] = kafka_position
                            plc_data['cn_name'] = cn_name
                            plc_data['device_a_tag'] = device_a_tag
                            plc_data['device_name'] = device_name

                        elif register_conf['type'] == 'bool':
                            plc_data[en_name] = int(self.plc.ReadBool(addr, num).Content[0])
                            plc_data['kafka_position'] = kafka_position
                            plc_data['cn_name'] = cn_name
                            plc_data['device_a_tag'] = device_a_tag
                            plc_data['device_name'] = device_name

                        elif register_conf['type'] == 'hex':
                            plc_data[en_name] = hex(self.plc.ReadUInt32(addr, num).Content[0])
                            plc_data['kafka_position'] = kafka_position
                            plc_data['cn_name'] = cn_name
                            plc_data['device_a_tag'] = device_a_tag
                            plc_data['device_name'] = device_name

                        tag_data.append(plc_data)
                        
                    except Exception as e:
                        Log().printError(f"读取PLC地址: {addr} 异常: {e}")
                        print(f"读取PLC地址: {addr} 异常: {e}")
                        continue
            
            return tag_data

        except Exception as e:
            # 如果连接断开，尝试重新连接
            self.plc = MelsecA1ENet(self.ip, self.port)
            self.plc.ConnectServer()
            Log().printError(f"{self.ip}读地址数据报错：{e}")
            print(f"{self.ip}地址数据报错：{e}")
            return []

    def write_plc(self, register_dict):
        """
        写入PLC数据
        
        Args:
            register_dict: 寄存器配置字典
        """
        # 确保连接成功
        while True:
            if not self.plc.ConnectServer().IsSuccess:
                Log().printError("PLC连接失败，正在重试...")
                time.sleep(1)
                continue
            else:
                break
        
        # 遍历寄存器配置，写入数据
        for register_name, register_conf in register_dict.items():
            try:
                num = register_conf['num']
                addr = register_conf['addr']
                value = register_conf['value']
                
                # 根据不同的数据类型写入数据
                if register_conf['type'] == 'str':
                    self.plc.WriteUnicodeString(addr, value, num)
                elif register_conf['type'] == 'int16':
                    self.plc.WriteInt16(addr, value)
                elif register_conf['type'] == 'int32':
                    self.plc.WriteInt32(addr, value)
                elif register_conf['type'] == 'float':
                    self.plc.WriteFloat(addr, value)
                elif register_conf['type'] == 'bool':
                    self.plc.WriteBool(addr, value)
                
                Log().printInfo(f"写入PLC地址: {addr}, 值: {value}, 类型: {register_conf['type']} 成功")
            except Exception as e:
                Log().printError(f'写入PLC {register_name} 失败: {e}')
                print(f'写入PLC {register_name} 失败: {e}')
        
        # 关闭连接
        self.plc.ConnectClose()

if __name__ == "__main__":
    # 测试代码
    ip = "192.168.3.251"
    port = 4998
    device_a_tag = "A_test"
    device_name = "test_device"
    plc_number = 0
    
    # 创建MelsecA1ENet客户端
    melsec_client = MelsecA1ENetClient(ip, port, device_a_tag, device_name, plc_number)
    
    # 测试寄存器配置
    register_dict = {
        "en_name": {
            'num': 1,
            'source_addr': "D100",
            'coefficient': 1,
            'kafka_position': "meatear",
            'precision': 2,
            'type': "int16",
            'cn_name': "test1"
        }
    }
    
    # 循环读取数据
    while True:
        tag_data = melsec_client.read_plc(register_dict)
        print(tag_data)
        time.sleep(1) 