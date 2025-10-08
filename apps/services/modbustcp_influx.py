#!/usr/bin/env python
# -*- encoding: utf-8 -*-

# here put the import lib
import time
import threading
import queue
from datetime import datetime
import ctypes
import inspect

from apps.connect.connect_influx import InfluxClient, w_influx
from apps.connect.connect_modbustcp import ModbustcpClient
from apps.utils.baseLogger import Log


def _async_raise(tid, exctype):
    """强制终止线程"""
    if not inspect.isclass(exctype):
        exctype = type(exctype)
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(tid), ctypes.py_object(exctype))
    if res == 0:
        raise ValueError("无效的线程ID")
    elif res != 1:
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(tid), None)
        raise SystemError("PyThreadState_SetAsyncExc 失败")


class ModbustcpInflux:
    def __init__(self, device_data_addresses):
        self.device_ip = device_data_addresses['source_ip']
        self.device_port = device_data_addresses['source_port']
        self.slave_addr = device_data_addresses.get('source_slave_addr', 1)
        self.device_conf = device_data_addresses
        self.device_fs = device_data_addresses.get('fs', 1)
        self.last_data_time = datetime.now()
        self.data_thread = None
        self.is_running = True
        self.data_buff = queue.Queue(maxsize=1000)
        Log().printInfo(f"Modbus TCP设备IP：{self.device_ip}：{self.device_port}")

    def modbustcp_influx(self):
        """
        主函数
        """
        modbustcp_client = ModbustcpClient(self.device_ip, self.device_port, self.device_conf)
        influxdb_client = InfluxClient().connect()

        threads = []

        self.data_thread = threading.Thread(target=self.get_data,
                                            args=(modbustcp_client,))
        threads.append(self.data_thread)
        # 数据计算和存储
        threads.append(threading.Thread(target=self.calc_and_save, args=(influxdb_client,)))

        for thread in threads:
            thread.start()

    def force_stop_thread(self, thread):
        """强制停止线程"""
        if thread and thread.is_alive():
            try:
                _async_raise(thread.ident, SystemExit)
                thread.join(timeout=1)
                Log().printInfo(f"Modbus TCP设备 {self.device_ip} 数据获取线程已强制终止")
            except Exception as e:
                Log().printError(f"强制终止线程失败: {e}")

    def restart_data_thread(self, device_conf):
        """重启数据获取线程"""
        # 强制终止旧线程
        if self.data_thread and self.data_thread.is_alive():
            self.force_stop_thread(self.data_thread)
        time.sleep(5)
        modbustcp_client = ModbustcpClient(self.device_ip, self.device_port)
        # 创建并启动新线程
        self.data_thread = threading.Thread(target=self.get_data, args=(modbustcp_client, device_conf, self.slave_addr))
        self.data_thread.start()
        Log().printInfo(f"Modbus TCP设备 {self.device_ip} 数据获取线程已重启")

    def get_data(self, modbustcp_client):
        """获取数据线程"""
        while True:
            try:
                tag_data = modbustcp_client.read_modbustcp()
                # print(f"{self.device_ip}:拿到数据:{tag_data}")
                # 成功获取数据
                if tag_data:
                    for data_item in tag_data:
                        self.data_buff.put(data_item)
            except Exception as e:
                Log().printError(f"Modbus TCP {self.device_ip}:{self.device_port} get_data函数报错: {e}")
                print(f"Modbus TCP {self.device_ip}:{self.device_port} get_data函数报错: {e}")
                time.sleep(5)  # 出错后等待5秒再重试

    def calc_and_save(self, influxdb_client):
        """计算和保存数据线程"""
        # 创建一个列表来存储积累的数据
        batch_data = []
        batch_size = 50  # 设置批量提交的数据量

        while True:
            try:
                tag_data = self.data_buff.get(timeout=60)  # 设置60秒超时
                self.last_data_time = datetime.now()

                # 提取数据字段
                kafka_position = tag_data.get('kafka_position', '')
                cn_name = tag_data.get('cn_name', '')
                device_a_tag = tag_data.get('device_a_tag', self.device_ip)
                device_name = tag_data.get('device_name', f'ModbusTCP_{self.device_ip}')

                # 移除非数据字段
                influx_data = tag_data.copy()
                for key in ['kafka_position', 'cn_name', 'device_a_tag', 'device_name']:
                    influx_data.pop(key, None)

                if not influx_data:
                    Log().printError("Modbus TCP data " + device_name + " is null")
                else:
                    # 将influx存储数据组织成对应的package存储格式
                    package = {
                        "measurement": device_a_tag,
                        "tags": {
                            "kafka_position": kafka_position,
                            "cn_name": cn_name,
                            "device_ip": self.device_ip,
                            "device_port": str(self.device_port)
                        },
                        "fields": influx_data
                    }

                    # 将数据添加到批处理列表中
                    batch_data.append(package)

                    # 当积累的数据达到指定数量时，一次性提交
                    if len(batch_data) >= batch_size:
                        w_influx(influxdb_client, device_name, batch_data)
                        # Log().printInfo(f"批量提交了 {len(batch_data)} 条Modbus TCP数据到InfluxDB")
                        batch_data = []

            except queue.Empty:
                # 如果60秒没有收到数据，强制重启数据获取线程
                Log().printWarning(f"Modbus TCP设备 {self.device_ip} 60秒未收到数据，准备强制重启数据获取线程")
                self.restart_data_thread(self.device_conf)
                continue
            except Exception as e:
                Log().printError(f"Modbus TCP {self.device_ip}:{self.device_port} calc_and_save函数报错: {e}")
                print(f"Modbus TCP {self.device_ip}:{self.device_port} calc_and_save函数报错: {e}")
                influxdb_client = InfluxClient().connect()

                # 如果发生错误但已有积累的数据，尝试提交这些数据
                if batch_data:
                    try:
                        w_influx(influxdb_client, device_name, batch_data)
                        Log().printInfo(f"错误恢复后提交了 {len(batch_data)} 条Modbus TCP数据到InfluxDB")
                        batch_data = []
                    except Exception as e2:
                        Log().printError(f"Modbus TCP {self.device_ip} 尝试提交积累数据时出错: {e2}")

