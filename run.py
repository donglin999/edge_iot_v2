#!/usr/bin/env python
# -*- encoding: utf-8 -*-

import pandas as pd
import multiprocessing
from settings import DevelopmentConfig
from apps.utils.baseLogger import Log
from apps.utils.excel_processor import process_excel_data
from apps.utils.process_manager import ProcessManager
from apps.utils.kafka_utils import get_kafka_config
# from apps.services.influx_kafka import InfluxKafka

def start_api_app(debug):
    """启动API服务"""
    if DevelopmentConfig().API_TAG == "API_KAFKA":
        from apps.apis.api_kafka_xt import Api_Kafka
        Log().printInfo("开始API采集")
        app = Api_Kafka(debug=debug)
        app.run()
    else:
        from apps.apis.api import Api
        Log().printInfo("开始API采集")
        app = Api(debug=debug)
        app.run()

def main():
    try:
        # 读取Excel配置
        df = pd.read_excel('数据地址清单.xlsx', sheet_name='Sheet1')
        device_data_addresses = process_excel_data(df)
        #print(f"device_data_addresses: {device_data_addresses}")
        # 创建进程管理器
        process_manager = ProcessManager()
        count = 0
        # 添加进程配置
        for config in device_data_addresses.values():
            print(f"启动第{count}个进程, 配置为:{config['protocol_type']}")
            process_type = config['protocol_type']
            process_manager.add_process_config(process_type, config)
        print(f"process_manager: {process_manager}")
        # 启动所有进程
        process_manager.start_all_processes()
        
        # 启动API进程
        if DevelopmentConfig().API_ENABLED:
            process_manager.start_process('API', {'debug': False})
            
        # 启动Kafka数据处理
        # if DevelopmentConfig().KAFKA_ENABLED:
        #     kafka_config = get_kafka_config(device_data_addresses)
        #     processor = InfluxKafka(
        #         influx_url=DevelopmentConfig().INFLUXDB_URL,
        #         influx_token=DevelopmentConfig().INFLUXDB_TOKEN,
        #         influx_org=DevelopmentConfig().INFLUXDB_ORG,
        #         influx_bucket=DevelopmentConfig().INFLUXDB_BUCKET,
        #         kafka_bootstrap_servers=DevelopmentConfig().kafka_bootstrap_servers,
        #         topic=DevelopmentConfig().kafka_topic
        #     )
        #     processor.process_and_send_data(kafka_config)
        # 启动进程监控
        #process_manager.monitor_processes()
        
    except Exception as e:
        Log().printError(f"程序运行出错: {e}")
        raise

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()


