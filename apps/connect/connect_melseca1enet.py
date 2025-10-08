#!/usr/bin/env python
# -*- encoding: utf-8 -*-

import json
import re
import time
from kafka import KafkaProducer
from datetime import datetime, timedelta, timezone
import struct
from apps.connect.connect_influx import w_influx, InfluxClient
from lib.HslCommunication import MelsecA1ENet
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
        self.ip = ip
        self.port = port
        self.plc_number = plc_number
        # self.device_a_tag = device_a_tag
        # self.device_name = device_name
        if DevelopmentConfig().KAFKA_ENABLED:
            self.kafka_producer = KafkaProducer(bootstrap_servers=DevelopmentConfig().kafka_bootstrap_servers,
                                                value_serializer=lambda x: json.dumps(x).encode('utf-8'))
            print(f"kafka连接成功，{DevelopmentConfig().kafka_bootstrap_servers}")
            Log().printInfo(f"kafka连接成功，{DevelopmentConfig().kafka_bootstrap_servers}")
        self.influxdb_client = InfluxClient().connect()
        Log().printInfo(f"influxdb对象创建成功")
        self.plc = MelsecA1ENet(self.ip, self.port)
        self.plc.PLCNumber = self.plc_number  # 设置PLC站号
        connect_result = self.plc.ConnectServer()
        if connect_result.IsSuccess:
            print(f"MelsecA1ENet连接成功，IP: {self.ip}, 端口: {self.port}")
            print("PLC连接成功!")
            print(f"连接信息: {connect_result.Message}")
        else:
            print(f"PLC连接失败!")
            print(f"错误信息: {connect_result.Message}")
            print(f"错误代码: {connect_result.ErrorCode}")
        self.count = 0
        print(f"尝试读数据")
        result = self.plc.ReadInt16("D5000", 1).Content[0]
        print(f"result:{result}")

    def contains_alpha_or_digit(self, s):
        # 检查是否包含字母
        contains_alpha = any(char.isalpha() for char in s)
        # 检查是否包含数字
        contains_digit = any(char.isdigit() for char in s)

        # 如果包含字母或数字，就打印消息
        if contains_alpha or contains_digit:
            print(f"{s}变量包含字母或数字")
        else:
            # 可选：如果不包含，也可以打印一条消息（这里省略）
            pass

    def read_plc(self, register_dict):
        try:
            tag_data = []

            # 按照数据类型分组寄存器
            register_groups = {
                'str': [],
                'int16': [],
                'int32': [],
                'float': [],
                'float2': [],
                'bool': [],
                'hex': []
            }

            # 记录每个寄存器的配置信息
            register_configs = {}

            # 分组寄存器
            for en_name, register_conf in register_dict.items():
                if isinstance(register_conf, dict):
                    data_type = register_conf['type']
                    if data_type in register_groups:
                        # 特殊处理codeResult
                        if data_type == 'str' and en_name == "codeResult":
                            # 单独处理codeResult
                            try:
                                num = int(register_conf['num'])
                                addr = register_conf['source_addr']
                                coefficient = register_conf['coefficient']
                                kafka_position = register_conf['kafka_position']
                                precision = int(register_conf['precision'])
                                cn_name = register_conf['cn_name']
                                device_a_tag = register_conf['device_a_tag']
                                device_name = register_conf['device_name']
                                read_data = str(self.plc.ReadString(addr, num).Content)[4:64]
                                # 检查是否包含字母或数字
                                contains_alpha = any(char.isalpha() for char in read_data)
                                contains_digit = any(char.isdigit() for char in read_data)

                                if contains_alpha or contains_digit:
                                    # print(f"{read_data}变量包含字母或数字")
                                    plc_data = {
                                        en_name: re.sub(r'\s+', '', read_data),
                                        'kafka_position': kafka_position,
                                        'cn_name': cn_name,
                                        'device_a_tag': device_a_tag,
                                        'device_name': device_name,
                                    }
                                else:
                                    plc_data = {
                                        en_name: "None",
                                        'kafka_position': kafka_position,
                                        'cn_name': cn_name,
                                        'device_a_tag': device_a_tag,
                                        'device_name': device_name,
                                    }
                                tag_data.append(plc_data)
                            except Exception as e:
                                Log().printError(f"read plc codeResult addr: {addr} Exception: {e}")
                                print(f"read plc codeResult addr: {addr} Exception: {e}")
                        else:
                            # 添加到对应的组
                            register_groups[data_type].append({
                                'en_name': en_name,
                                'addr': register_conf['source_addr'],
                                'num': int(register_conf['num'])
                            })
                            # 保存配置信息
                            register_configs[en_name] = register_conf

            # 批量读取各类型寄存器
            # 处理字符串类型
            for reg in register_groups['str']:
                try:
                    en_name = reg['en_name']
                    addr = reg['addr']
                    num = reg['num']
                    conf = register_configs[en_name]

                    plc_data = {
                        en_name: str(self.plc.ReadString(addr, num).Content),
                        'kafka_position': conf['kafka_position'],
                        'cn_name': conf['cn_name'],
                        'device_a_tag': conf['device_a_tag'],
                        'device_name': conf['device_name']
                    }
                    tag_data.append(plc_data)
                except Exception as e:
                    Log().printError(f"read plc str addr: {addr} Exception: {e}")
                    print(f"read plc str addr: {addr} Exception: {e}")

            # 处理int16类型 - 批量读取
            if register_groups['int16']:
                # 找出连续的地址
                continuous_groups = self._group_continuous_registers(register_groups['int16'], 'D')

                for group in continuous_groups:
                    if not group:
                        continue

                    # 计算起始地址和长度
                    start_addr = group[0]['addr']
                    total_length = group[-1]['addr_num'] - group[0]['addr_num'] + group[-1]['num']
                    print(f"start_addr:{start_addr},total_length:{total_length}")
                    try:
                        # 批量读取
                        # try:
                        #     result = self.plc.Read(start_addr, total_length)
                        # except Exception as e:
                        #     print(f"read plc str addr: {addr} Exception: {e}")
                        #     print(f"result:{result.IsSuccess},{result.Content}")
                        # if result.IsSuccess:
                        #     # 处理结果
                        #     for i, reg in enumerate(group):
                        #         en_name = reg['en_name']
                        #         conf = register_configs[en_name]
                        #         offset = reg['addr_num'] - group[0]['addr_num']
                        #
                        #         value = result.Content[offset * 2] + result.Content[offset * 2 + 1] * 256
                        #         if value > 32767:  # 处理有符号整数
                        #             value = value - 65536
                        #
                        #         plc_data = {
                        #             en_name: int(round(value * float(conf['coefficient']), int(conf['precision']))),
                        #             'kafka_position': conf['kafka_position'],
                        #             'cn_name': conf['cn_name'],
                        #             'device_a_tag': conf['device_a_tag'],
                        #             'device_name': conf['device_name']
                        #         }
                        #         tag_data.append(plc_data)
                        # else:
                            # 如果批量读取失败，回退到单个读取
                            for reg in group:
                                en_name = reg['en_name']
                                addr = reg['addr']
                                conf = register_configs[en_name]
                                result = self.plc.ReadInt16(addr, reg['num'])
                                # result = self.plc.ReadInt16("D5000", 1).Content[0]
                                # print(f"result:{result}")
                                if result.IsSuccess:
                                    plc_data = {
                                        en_name: int(round(result.Content[0] * float(conf['coefficient']),
                                                           int(conf['precision']))),
                                        'kafka_position': conf['kafka_position'],
                                        'cn_name': conf['cn_name'],
                                        'device_a_tag': conf['device_a_tag'],
                                        'device_name': conf['device_name']
                                    }
                                    # tag_data.append(plc_data)
                    except Exception as e:
                        Log().printError(f"read plc int16 batch addr: {start_addr} Exception: {e}")
                        print(f"read plc int16 batch addr: {start_addr} Exception: {e}")
                        # 回退到单个读取
                        for reg in group:
                            try:
                                en_name = reg['en_name']
                                addr = reg['addr']
                                conf = register_configs[en_name]

                                result = self.plc.ReadInt16(addr, 1)
                                if result.IsSuccess:
                                    plc_data = {
                                        en_name: int(round(result.Content[0] * float(conf['coefficient']),
                                                           int(conf['precision']))),
                                        'kafka_position': conf['kafka_position'],
                                        'cn_name': conf['cn_name'],
                                        'device_a_tag': conf['device_a_tag'],
                                        'device_name': conf['device_name']
                                    }
                                    tag_data.append(plc_data)
                            except Exception as e:
                                Log().printError(f"read plc int16 addr: {addr} Exception: {e}")
                                print(f"read plc int16 addr: {addr} Exception: {e}")

            # 处理int32类型 - 批量读取
            if register_groups['int32']:
                continuous_groups = self._group_continuous_registers(register_groups['int32'], 'D')

                for group in continuous_groups:
                    if not group:
                        continue

                    start_addr = group[0]['addr']
                    total_length = (group[-1]['addr_num'] - group[0]['addr_num'] + group[-1]['num']) * 2

                    try:
                        result = self.plc.Read(start_addr, total_length)
                        if result.IsSuccess:
                            for i, reg in enumerate(group):
                                en_name = reg['en_name']
                                conf = register_configs[en_name]
                                offset = (reg['addr_num'] - group[0]['addr_num']) * 2

                                value = result.Content[offset] + result.Content[offset + 1] * 256 + \
                                        result.Content[offset + 2] * 65536 + result.Content[offset + 3] * 16777216
                                if value > 2147483647:  # 处理有符号整数
                                    value = value - 4294967296

                                plc_data = {
                                    en_name: round(value * float(conf['coefficient']), int(conf['precision'])),
                                    'kafka_position': conf['kafka_position'],
                                    'cn_name': conf['cn_name'],
                                    'device_a_tag': conf['device_a_tag'],
                                    'device_name': conf['device_name']
                                }
                                tag_data.append(plc_data)
                        else:
                            # 回退到单个读取
                            for reg in group:
                                en_name = reg['en_name']
                                addr = reg['addr']
                                conf = register_configs[en_name]

                                result = self.plc.ReadInt32(addr, 1)
                                if result.IsSuccess:
                                    plc_data = {
                                        en_name: round(result.Content[0] * float(conf['coefficient']),
                                                       int(conf['precision'])),
                                        'kafka_position': conf['kafka_position'],
                                        'cn_name': conf['cn_name'],
                                        'device_a_tag': conf['device_a_tag'],
                                        'device_name': conf['device_name']
                                    }
                                    tag_data.append(plc_data)
                    except Exception as e:
                        Log().printError(f"read plc int32 batch addr: {start_addr} Exception: {e}")
                        print(f"read plc int32 batch addr: {start_addr} Exception: {e}")
                        # 回退到单个读取
                        for reg in group:
                            try:
                                en_name = reg['en_name']
                                addr = reg['addr']
                                conf = register_configs[en_name]

                                result = self.plc.ReadInt32(addr, 1)
                                if result.IsSuccess:
                                    plc_data = {
                                        en_name: round(result.Content[0] * float(conf['coefficient']),
                                                       int(conf['precision'])),
                                        'kafka_position': conf['kafka_position'],
                                        'cn_name': conf['cn_name'],
                                        'device_a_tag': conf['device_a_tag'],
                                        'device_name': conf['device_name']
                                    }
                                    tag_data.append(plc_data)
                            except Exception as e:
                                Log().printError(f"read plc int32 addr: {addr} Exception: {e}")
                                print(f"read plc int32 addr: {addr} Exception: {e}")

            # 处理float类型 - 批量读取
            if register_groups['float']:
                continuous_groups = self._group_continuous_registers(register_groups['float'], 'D')

                for group in continuous_groups:
                    if not group:
                        continue

                    start_addr = group[0]['addr']
                    total_length = (group[-1]['addr_num'] - group[0]['addr_num'] + group[-1]['num']) * 2

                    try:
                        result = self.plc.Read(start_addr, total_length)
                        if result.IsSuccess:
                            for i, reg in enumerate(group):
                                en_name = reg['en_name']
                                conf = register_configs[en_name]
                                offset = (reg['addr_num'] - group[0]['addr_num']) * 2

                                # 从字节数组中提取浮点数
                                byte_array = bytearray(4)
                                byte_array[0] = result.Content[offset]
                                byte_array[1] = result.Content[offset + 1]
                                byte_array[2] = result.Content[offset + 2]
                                byte_array[3] = result.Content[offset + 3]
                                value = struct.unpack('<f', byte_array)[0]

                                plc_data = {
                                    en_name: round(value * float(conf['coefficient']), int(conf['precision'])),
                                    'kafka_position': conf['kafka_position'],
                                    'cn_name': conf['cn_name'],
                                    'device_a_tag': conf['device_a_tag'],
                                    'device_name': conf['device_name']
                                }
                                tag_data.append(plc_data)
                        else:
                            # 回退到单个读取
                            for reg in group:
                                en_name = reg['en_name']
                                addr = reg['addr']
                                conf = register_configs[en_name]

                                result = self.plc.ReadFloat(addr, 1)
                                if result.IsSuccess:
                                    plc_data = {
                                        en_name: round(result.Content[0] * float(conf['coefficient']),
                                                       int(conf['precision'])),
                                        'kafka_position': conf['kafka_position'],
                                        'cn_name': conf['cn_name'],
                                        'device_a_tag': conf['device_a_tag'],
                                        'device_name': conf['device_name']
                                    }
                                    tag_data.append(plc_data)
                    except Exception as e:
                        Log().printError(f"read plc float batch addr: {start_addr} Exception: {e}")
                        print(f"read plc float batch addr: {start_addr} Exception: {e}")
                        # 回退到单个读取
                        for reg in group:
                            try:
                                en_name = reg['en_name']
                                addr = reg['addr']
                                conf = register_configs[en_name]

                                result = self.plc.ReadFloat(addr, 1)
                                if result.IsSuccess:
                                    plc_data = {
                                        en_name: round(result.Content[0] * float(conf['coefficient']),
                                                       int(conf['precision'])),
                                        'kafka_position': conf['kafka_position'],
                                        'cn_name': conf['cn_name'],
                                        'device_a_tag': conf['device_a_tag'],
                                        'device_name': conf['device_name']
                                    }
                                    tag_data.append(plc_data)
                            except Exception as e:
                                Log().printError(f"read plc float addr: {addr} Exception: {e}")
                                print(f"read plc float addr: {addr} Exception: {e}")

            # 处理float2类型
            for reg in register_groups['float2']:
                try:
                    en_name = reg['en_name']
                    addr = reg['addr']
                    conf = register_configs[en_name]

                    value = self.plc.ReadUInt32(addr, 1).Content[0]
                    value2 = circular_shift_left(value, 16)
                    value3 = struct.unpack('<f', struct.pack('<I', value2))[0]

                    plc_data = {
                        en_name: round(value3, int(conf['precision'])),
                        'kafka_position': conf['kafka_position'],
                        'cn_name': conf['cn_name'],
                        'device_a_tag': conf['device_a_tag'],
                        'device_name': conf['device_name']
                    }
                    tag_data.append(plc_data)
                except Exception as e:
                    Log().printError(f"read plc float2 addr: {addr} Exception: {e}")
                    print(f"read plc float2 addr: {addr} Exception: {e}")

            # 处理bool类型 - 批量读取
            if register_groups['bool']:
                # 按照位地址前缀分组（如X、Y、M等）
                bool_prefix_groups = {}
                for reg in register_groups['bool']:
                    prefix = reg['addr'][0]  # 获取地址前缀，如X、Y、M等
                    if prefix not in bool_prefix_groups:
                        bool_prefix_groups[prefix] = []
                    bool_prefix_groups[prefix].append(reg)

                # 对每个前缀组内的地址进行批量读取
                for prefix, regs in bool_prefix_groups.items():
                    continuous_groups = self._group_continuous_bool_registers(regs)

                    for group in continuous_groups:
                        if not group:
                            continue

                        start_addr = group[0]['addr']
                        total_length = group[-1]['addr_num'] - group[0]['addr_num'] + 1

                        try:
                            result = self.plc.ReadBool(start_addr, total_length)
                            if result.IsSuccess:
                                for i, reg in enumerate(group):
                                    en_name = reg['en_name']
                                    conf = register_configs[en_name]
                                    offset = reg['addr_num'] - group[0]['addr_num']

                                    plc_data = {
                                        en_name: int(result.Content[offset]),
                                        'kafka_position': conf['kafka_position'],
                                        'cn_name': conf['cn_name'],
                                        'device_a_tag': conf['device_a_tag'],
                                        'device_name': conf['device_name']
                                    }
                                    tag_data.append(plc_data)
                            else:
                                # 回退到单个读取
                                for reg in group:
                                    en_name = reg['en_name']
                                    addr = reg['addr']
                                    conf = register_configs[en_name]

                                    result = self.plc.ReadBool(addr, 1)
                                    if result.IsSuccess:
                                        plc_data = {
                                            en_name: int(result.Content[0]),
                                            'kafka_position': conf['kafka_position'],
                                            'cn_name': conf['cn_name']
                                        }
                                        tag_data.append(plc_data)
                        except Exception as e:
                            Log().printError(f"read plc bool batch addr: {start_addr} Exception: {e}")
                            print(f"read plc bool batch addr: {start_addr} Exception: {e}")
                            # 回退到单个读取
                            for reg in group:
                                try:
                                    en_name = reg['en_name']
                                    addr = reg['addr']
                                    conf = register_configs[en_name]

                                    result = self.plc.ReadBool(addr, 1)
                                    if result.IsSuccess:
                                        plc_data = {
                                            en_name: int(result.Content[0]),
                                            'kafka_position': conf['kafka_position'],
                                            'cn_name': conf['cn_name'],
                                            'device_a_tag': conf['device_a_tag'],
                                            'device_name': conf['device_name']
                                        }
                                        tag_data.append(plc_data)
                                except Exception as e:
                                    Log().printError(f"read plc bool addr: {addr} Exception: {e}")
                                    print(f"read plc bool addr: {addr} Exception: {e}")

            # 处理hex类型
            for reg in register_groups['hex']:
                try:
                    en_name = reg['en_name']
                    addr = reg['addr']
                    conf = register_configs[en_name]

                    plc_data = {
                        en_name: hex(self.plc.ReadUInt32(addr, 1).Content[0]),
                        'kafka_position': conf['kafka_position'],
                        'cn_name': conf['cn_name'],
                        'device_a_tag': conf['device_a_tag'],
                        'device_name': conf['device_name']
                    }
                    tag_data.append(plc_data)
                except Exception as e:
                    Log().printError(f"read plc hex addr: {addr} Exception: {e}")
                    print(f"read plc hex addr: {addr} Exception: {e}")

            return tag_data

        except Exception as e:
            # self.plc.ConnectClose()
            self.plc = MelsecMcNet(self.ip, self.port)
            self.plc.ConnectServer()
            Log().printError(f"{self.ip}读地址数据报错：{e}")
            print(f"{self.ip}地址数据报错：{e}")
            return []

    def _group_continuous_registers(self, registers, prefix_type='D'):
        """
        将连续的寄存器分组，以便批量读取

        Args:
            registers: 寄存器列表
            prefix_type: 寄存器前缀类型，如'D'、'X'等

        Returns:
            连续寄存器组列表
        """
        if not registers:
            return []

        # 解析地址，提取数字部分
        for reg in registers:
            addr = reg['addr']
            # 提取地址中的数字部分
            if addr.startswith(prefix_type):
                try:
                    reg['addr_num'] = int(addr[len(prefix_type):])
                except ValueError:
                    # 如果无法解析为数字，则设置一个特殊值
                    reg['addr_num'] = -1
            else:
                reg['addr_num'] = -1

        # 按地址数字部分排序
        sorted_regs = sorted([r for r in registers if r['addr_num'] != -1], key=lambda x: x['addr_num'])

        if not sorted_regs:
            return []

        # 分组连续的寄存器
        groups = []
        current_group = [sorted_regs[0]]

        for i in range(1, len(sorted_regs)):
            prev_reg = current_group[-1]
            curr_reg = sorted_regs[i]

            # 检查是否连续（考虑前一个寄存器的长度）
            if curr_reg['addr_num'] <= prev_reg['addr_num'] + prev_reg['num']:
                # 连续，添加到当前组
                current_group.append(curr_reg)
            else:
                # 不连续，创建新组
                groups.append(current_group)
                current_group = [curr_reg]

        # 添加最后一组
        if current_group:
            groups.append(current_group)

        return groups

    def _group_continuous_bool_registers(self, registers):
        """
        将连续的布尔寄存器分组，以便批量读取

        Args:
            registers: 布尔寄存器列表

        Returns:
            连续布尔寄存器组列表
        """
        if not registers:
            return []

        # 解析地址，提取前缀和数字部分
        for reg in registers:
            addr = reg['addr']
            # 提取地址中的前缀和数字部分
            match = re.match(r'([A-Za-z]+)(\d+)', addr)
            if match:
                prefix, num_str = match.groups()
                try:
                    reg['prefix'] = prefix
                    reg['addr_num'] = int(num_str)
                except ValueError:
                    reg['prefix'] = ''
                    reg['addr_num'] = -1
            else:
                reg['prefix'] = ''
                reg['addr_num'] = -1

        # 按前缀和地址数字部分分组
        prefix_groups = {}
        for reg in registers:
            if reg['prefix'] and reg['addr_num'] != -1:
                if reg['prefix'] not in prefix_groups:
                    prefix_groups[reg['prefix']] = []
                prefix_groups[reg['prefix']].append(reg)

        # 对每个前缀组内的寄存器按地址排序并分组连续的
        all_groups = []
        for prefix, regs in prefix_groups.items():
            sorted_regs = sorted(regs, key=lambda x: x['addr_num'])

            groups = []
            current_group = [sorted_regs[0]]

            for i in range(1, len(sorted_regs)):
                prev_reg = current_group[-1]
                curr_reg = sorted_regs[i]

                # 检查是否连续
                if curr_reg['addr_num'] == prev_reg['addr_num'] + 1:
                    # 连续，添加到当前组
                    current_group.append(curr_reg)
                else:
                    # 不连续，创建新组
                    groups.append(current_group)
                    current_group = [curr_reg]

            # 添加最后一组
            if current_group:
                groups.append(current_group)

            all_groups.extend(groups)

        return all_groups

    def write_plc(self, register_dict):
        while True:
            if not self.plc.ConnectServer().IsSuccess:
                Log().printError("PLC connect !")
                time.sleep(1)
                continue
            else:
                break

        for register_name, register_conf in register_dict.items():
            try:
                num = register_conf['num']
                addr = register_conf['addr']
                value = register_conf['value']

                if register_conf['type'] == 'str':
                    self.plc.WriteUnicodeString(addr, value, num)
                elif register_conf['type'] == 'int16':
                    self.plc.WriteInt16(addr, value)
                elif register_conf['type'] == 'float':
                    self.plc.WriteFloat(addr, value)
            except Exception as e:
                Log().printError('write plc ' + register_name + ' failed')
                Log().printError(e)
        self.plc.ConnectClose()

