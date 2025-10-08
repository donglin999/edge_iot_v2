#!/usr/bin/env python
# -*- encoding: utf-8 -*-

from modbus_tk import modbus_tcp
import modbus_tk.defines as cst
from apps.utils.baseLogger import Log
import time


class ModbustcpClient:
    def __init__(self, ip, port, register_dict) -> None:
        self.ip = ip
        self.port = port
        self.master = None
        try:
            self.connect()
            self.register_configs = self._group_continuous_registers(register_dict)
        except Exception as e:
            Log().printError(f"Modbus TCP连接失败 {self.ip}:{self.port}, 错误: {e}")
            raise e

    def connect(self):
        """连接到Modbus TCP设备"""
        try:
            self.master = modbus_tcp.TcpMaster(self.ip, port=self.port, timeout_in_sec=10)
            Log().printInfo(f"成功连接到Modbus TCP设备 {self.ip}:{self.port}")
            return True
        except Exception as e:
            Log().printError(f"连接Modbus TCP设备失败 {self.ip}:{self.port}, 错误: {e}")
            return False

    def _group_continuous_registers(self, registers):
        """将连续的寄存器地址分组"""
        try:
            # 首先按功能码分组
            function_groups = {}
            for en_name, register_conf in registers.items():
                if isinstance(register_conf, dict):
                    func_code = int(register_conf['type'])
                    if func_code not in function_groups:
                        function_groups[func_code] = []
                    function_groups[func_code].append(register_conf)

            # 对每个功能码组内的寄存器按地址排序
            for func_code in function_groups:
                function_groups[func_code].sort(key=lambda x: x['source_addr'])

            # 对每个功能码组内的寄存器进行连续分组
            continuous_groups = {}
            for func_code, regs in function_groups.items():
                continuous_groups[func_code] = []
                current_group = []

                for i, reg in enumerate(regs):
                    if not current_group:
                        current_group.append(reg)
                    else:
                        # 检查是否连续
                        last_reg = current_group[-1]
                        expected_addr = last_reg['source_addr'] + last_reg['num']

                        if reg['source_addr'] == expected_addr:
                            current_group.append(reg)
                        else:
                            if current_group:
                                continuous_groups[func_code].append(current_group)
                            current_group = [reg]

                if current_group:
                    continuous_groups[func_code].append(current_group)

            return continuous_groups
        except Exception as e:
            Log().printError(f"将连续的寄存器地址分组失败 {self.ip}:{self.port}, 错误: {e}")
            print(f"将连续的寄存器地址分组失败 {self.ip}:{self.port}, 错误: {e}")
            time.sleep(10)
            return None

    def read_modbustcp(self, slave_addr=1):
        """读取Modbus TCP数据，支持连续地址批量读取"""
        if not self.master:
            if not self.connect():
                return None

        tag_data = []

        # 收集所有需要读取的寄存器信息
        # 获取连续分组

        try:
            for func_code, groups in self.register_configs.items():
                for group in groups:
                    start_addr = group[0]['source_addr']
                    total_length = (group[-1]['source_addr'] + group[-1]['num']) - start_addr

                    try:
                        # 批量读取数据
                        data = self.master.execute(
                            slave=slave_addr,
                            function_code=func_code,
                            starting_address=start_addr,
                            quantity_of_x=total_length
                        )

                        # 分配数据到各个寄存器
                        offset = 0
                        for reg in group:
                            reg_length = reg['num']
                            reg_data = data[offset:offset + reg_length]
                            offset += reg_length

                            # 构建数据字典
                            modbustcp_data = {
                                reg['en_name']: reg_data[0] if len(reg_data) == 1 else list(reg_data),
                                'cn_name': reg['cn_name'],
                                'device_a_tag': reg['device_a_tag'],
                                'device_name': reg['device_name'],
                            }

                            # 添加可选字段
                            if 'kafka_position' in reg:
                                modbustcp_data['kafka_position'] = reg['kafka_position']

                            tag_data.append(modbustcp_data)


                    except Exception as e:
                        Log().printError(
                            f"读取寄存器失败 - 起始地址: {start_addr}, 长度: {total_length}, 功能码: {func_code}, 错误: {e}")
                        continue

        except Exception as e:
            Log().printError(f"读取Modbus TCP数据总体错误: {e}")
            return None
        # print(f"conncet get data :{tag_data}")


        return tag_data

    def close(self):
        """关闭连接"""
        if self.master:
            try:
                self.master.close()
                self.master = None
                Log().printInfo(f"已关闭与Modbus TCP设备的连接 {self.ip}:{self.port}")
            except Exception as e:
                Log().printError(f"关闭Modbus TCP连接失败: {e}")
