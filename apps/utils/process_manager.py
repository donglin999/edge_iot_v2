import multiprocessing
from multiprocessing import Process
import threading
import time
from apps.utils.baseLogger import Log
from settings import DevelopmentConfig


class ProcessManager:
    """进程管理器"""

    def __init__(self):
        self.processes = {}
        self.process_configs = {}

    def add_process_config(self, process_type, config):
        """添加进程配置"""
        if process_type not in self.process_configs:
            self.process_configs[process_type] = []
        self.process_configs[process_type].append(config)

    def start_process(self, process_type, config):
        #print(f"start_process: {process_type}, config: {config}")
        """启动单个进程"""
        try:
            if process_type == 'MC':
                from apps.services.plc_influx import PlcInflux
                process = Process(target=PlcInflux(config).plc_influx)
            elif process_type == 'modbustcp':
                from apps.services.modbustcp_influx import ModbustcpInflux
                process = Process(target=ModbustcpInflux(config).modbustcp_influx)
            elif process_type in ['MQTT', 'mqtt']:
                from apps.services.mqtt_influx import MqttInflux
                process = Process(target=MqttInflux(config).mqtt_influx)
            elif process_type == 'MQTT_SIEMENS':
                from apps.services.mqtt_siemens import MqttSiemens
                process = Process(target=MqttSiemens(config).mqtt_siemens)
            elif process_type == 'modbustcp_mqtt':
                from apps.services.modbustcp_mqtt import ModbustcpMQTT
                process = Process(target=ModbustcpMQTT(config).modbustcp_mqtt)
            elif process_type == 'VK':
                from apps.services.vk_influx import VKInflux
                process = Process(target=VKInflux(config).vk_influx, args=(config,))
            elif process_type == 'VK701NDC':
                from apps.services.vk_influx_701NDC import VK701NDC
                vk = VK701NDC(config)
                vk.server_init()
                process = Process(target=vk.vk701NDC_influx, args=(vk, config))
            elif process_type == 'melseca1enet':
                from apps.services.melseca1enet_influx import MelsecA1ENetInflux
                process = Process(target=MelsecA1ENetInflux(config).melseca1enet_influx)
            elif process_type == 'kafka':
                from apps.services.kafka_hmi import KafkaHMI
                kafka_config = {
                    'bootstrap_servers': f"{config['source_ip']}:{config['source_port']}",
                    'topic': config['device_name'],
                    'hmi_ip': DevelopmentConfig().hmi_ip,
                    'hmi_port': DevelopmentConfig().hmi_port
                }
                process = Process(target=KafkaHMI(**kafka_config).kafka_hmi)
            elif process_type == "TCP_client_test":
                from test.tcp_client_test import TCPClientTest
                process = Process(target=TCPClientTest(config).start)
            elif process_type == 'TCP_client':
                from apps.services.tcp_services import TCPServer
                process = Process(target=TCPServer(config).start)
            elif process_type == 'API':
                if DevelopmentConfig().API_TAG == "API_KAFKA":
                    from apps.apis.api_kafka_xt import Api_Kafka
                    process = Process(target=Api_Kafka(debug=config['debug']).run)
                else:
                    from apps.apis.api import Api
                    process = Process(target=Api(debug=config['debug']).run)
            else:
                Log().printError(f"未知的进程类型: {process_type}")
                return None
            #print(f"start_process: {process_type}, config: {config}")

            process.start()
            self.processes[process.pid] = {
                'process': process,
                'type': process_type,
                'config': config
            }
            Log().printInfo(f"启动{process_type}进程成功, PID: {process.pid}")
            print(f"启动{process_type}进程成功, PID: {process.pid}")
            return process.pid

        except Exception as e:
            Log().printError(f"启动{process_type}进程失败: {e}")
            print(f"启动{process_type}进程失败: {e}")
            return None

    def start_all_processes(self):
        """启动所有配置的进程"""
        for process_type, configs in self.process_configs.items():
            for config in configs:
                Log().printInfo(f"process_type: {process_type}")
                print(f"process_type: {process_type}")
                self.start_process(process_type, config)

    def monitor_processes(self):
        pass
        """监控进程状态"""
        while True:
            for pid, info in list(self.processes.items()):
                if not info['process'].is_alive():
                    Log().printInfo(f"进程 {pid} ({info['type']}) 已终止,尝试重启")
                    print(f"进程 {pid} ({info['type']}) 已终止,尝试重启")
                    self.start_process(info['type'], info['config'])
                    del self.processes[pid]
            time.sleep(5)