#!/usr/bin/env python
# -*- encoding: utf-8 -*-
'''
@Time    :   2024/04/18 16:22:15
@Author  :   lihj210
@Version :   1.0
@Contact :   lihj210@midea.com
@Desc    :   系统级的参数配置
'''

#  import lib
import os

basedir = os.path.abspath(os.path.dirname(__file__))

class Config:
    SECRET_KEY = os.getenv('SECRET_KEY', 'my_precious_secret_key')
    DEBUG = True
    LOG_FILE = "/apps/data_acquisition/Logs/"

class VKConfig():

    TCP_PORT_1 = 8235
    TCP_PORT_2 = 8230
    TCP_PORT_3 = 8231
    TCP_PORT_4 = 8232
    TCP_PORT_5 = 8233
    TCP_PORT_6 = 8234

    TRY_NUM = 2000

    DAQ_SET = [0, 0, 0, 0, 1, 0, 1, 0, 1, 0, 1, 0, 0, 0, 0, 0]
    '''
    DAQ_NUM = {
        "192.168.1.191": 0,
        "192.168.1.192": 1,
        "192.168.1.193": 2,
        "192.168.1.194": 3,
        "192.168.1.195": 4,
    }
    '''

    DAQ_NUM = 0
    FS = 10000
    # 设置缓存区大小
    SAMPLE_NUM = 30000
    SENSOR_NUM = 8
    SAMPLE_PERIOD = 3
    ADC = 16
    DAQ_MODE = 1
    MACHINE_ID_1 = 8_77
    DATA_SAVE = 10800
    # 设置通道数量
    CHANNEL_NUM = 4

class VK701NDCConfig():
    save_enable = False

    vkconfig = {
        0: {
            # 采集卡基本信息
            'ip': '',
            # 通道个数
            'channel_num': 4,
            # 设置采样频率
            'fs': 10000,
            # 设置参考电压
            'voltage': 4,
            # 设置精度
            'accuracy': 24,
            # 设置n采样点数
            'samplenumber': 10000,
            # 设置通道参数，设置方法参考sdk文档
            'params': [1, 1, 1, 1, 1, 1, 1, 1],
            # 设置采集模式，默认为1
            'DAQ_MODE': 1,
            # 配置vk各个通道连接的传感器信息
            'sensor_conversion_info':
                {0: 'data*100',
                 1: 'data*100',
                 2: 'data*100',
                 3: 'data*100',
                 },
            # 配置vk各个通道需要计算的特征信息
            'feature_calcu_info':
                {0: [('acc_rms', ()), ('vel_rms', ())],
                 1: [('acc_rms', ()), ('vel_rms', ())],
                 2: [('acc_rms', ()), ('vel_rms', ())],
                 3: [('acc_rms', ()), ('vel_rms', ())],
                 }
        },
        1: {
            # 采集卡id
            'id': 0,
            'ip': '',
            # 通道个数
            'channel_num': 4,
            # 设置采样频率
            'fs': 10000,
            # 设置参考电压
            'voltage': 4,
            # 设置精度
            'accuracy': 24,
            # 设置n采样点数
            'samplenumber': 10000,
            # 设置通道参数，设置方法参考sdk文档
            'params': [1, 1, 1, 1, 1, 1, 1, 1],
            # 设置采集模式，默认为1
            'DAQ_MODE': 1,
            # 配置vk各个通道连接的传感器信息
            'sensor_conversion_info':
                {0: 'data*100',
                 1: 'data*100',
                 2: 'data*100',
                 3: 'data*100',
                 },
            # 配置vk各个通道需要计算的特征信息
            'feature_calcu_info':
                {0: [('acc_rms', ()), ('vel_rms', ())],
                 1: [('acc_rms', ()), ('vel_rms', ())],
                 2: [('acc_rms', ()), ('vel_rms', ())],
                 3: [('acc_rms', ()), ('vel_rms', ())],
                 }
        },
        2: {
            # 采集卡id
            'id': 0,
            'ip': '',
            # 通道个数
            'channel_num': 4,
            # 设置采样频率
            'fs': 10000,
            # 设置参考电压
            'voltage': 4,
            # 设置精度
            'accuracy': 24,
            # 设置n采样点数
            'samplenumber': 10000,
            # 设置通道参数，设置方法参考sdk文档
            'params': [1, 1, 1, 1, 1, 1, 1, 1],
            # 设置采集模式，默认为1
            'DAQ_MODE': 1,
            # 配置vk各个通道连接的传感器信息
            'sensor_conversion_info':
                {0: 'data*100',
                 1: 'data*100',
                 2: 'data*100',
                 3: 'data*100',
                 },
            # 配置vk各个通道需要计算的特征信息
            'feature_calcu_info':
                {0: [('acc_rms', ()), ('vel_rms', ())],
                 1: [('acc_rms', ()), ('vel_rms', ())],
                 2: [('acc_rms', ()), ('vel_rms', ())],
                 3: [('acc_rms', ()), ('vel_rms', ())],
                 }
        },
        3: {
            # 采集卡id
            'id': 0,
            'ip': '',
            # 通道个数
            'channel_num': 4,
            # 设置采样频率
            'fs': 10000,
            # 设置参考电压
            'voltage': 4,
            # 设置精度
            'accuracy': 24,
            # 设置n采样点数
            'samplenumber': 10000,
            # 设置通道参数，设置方法参考sdk文档
            'params': [1, 1, 1, 1, 1, 1, 1, 1],
            # 设置采集模式，默认为1
            'DAQ_MODE': 1,
            # 配置vk各个通道连接的传感器信息
            'sensor_conversion_info':
                {0: 'data*100',
                 1: 'data*100',
                 2: 'data*100',
                 3: 'data*100',
                 },
            # 配置vk各个通道需要计算的特征信息
            'feature_calcu_info':
                {0: [('acc_rms', ()), ('vel_rms', ())],
                 1: [('acc_rms', ()), ('vel_rms', ())],
                 2: [('acc_rms', ()), ('vel_rms', ())],
                 3: [('acc_rms', ()), ('vel_rms', ())],
                 }
        },
        4: {
            # 采集卡id
            'id': 0,
            'ip': '',
            # 通道个数
            'channel_num': 4,
            # 设置采样频率
            'fs': 10000,
            # 设置参考电压
            'voltage': 4,
            # 设置精度
            'accuracy': 24,
            # 设置n采样点数
            'samplenumber': 10000,
            # 设置通道参数，设置方法参考sdk文档
            'params': [1, 1, 1, 1, 1, 1, 1, 1],
            # 设置采集模式，默认为1
            'DAQ_MODE': 1,
            # 配置vk各个通道连接的传感器信息
            'sensor_conversion_info':
                {0: 'data*100',
                 1: 'data*100',
                 2: 'data*100',
                 3: 'data*100',
                 },
            # 配置vk各个通道需要计算的特征信息
            'feature_calcu_info':
                {0: [('acc_rms', ()), ('vel_rms', ())],
                 1: [('acc_rms', ()), ('vel_rms', ())],
                 2: [('acc_rms', ()), ('vel_rms', ())],
                 3: [('acc_rms', ()), ('vel_rms', ())],
                 }
        },
    }

# 开发环境配置
class DevelopmentConfig(Config):
    """项目配置核心类"""
    DEBUG = False
    # 配置日志
    # LOG_LEVEL = "DEBUG"
    LOG_LEVEL = "INFO"

    PLC_IP = "192.168.1.50"

    hmi_ip = '192.168.8.65'
    hmi_port = 502

    # 配置MQTT
    # 项目上线以后，这个地址就会被替换成真实IP地址，mysql也是
    MQTT_ENABLED = False
    MQTT_HOST = '10.18.62.24'
    MQTT_PORT = 8883
    MQTT_POLL = 10
    MQTT_USERNAME = 'ZYY_GYSD'
    MQTT_PASSWORD = '8e92419069c74b8ea253795b88f8c546'
    MQTT_TOPIC = "/IMRC"
    device_mqtt_topic_start = "/sys/33fb0a8e1c27460a8a8bc21d265a1729/device/",
    device_mqtt_topic_end = "/thing/property/#",

    # MQTT_BROKER_URL = '10.141.1.222'
    # MQTT_BROKER_PORT = 1883
    # MQTT_PASSWORD = ''
    # MQTT_POLL = 10


    # 配置INFLUXDB
    # 项目上线以后，这个地址就会被替换成真实IP地址，mysql也是
    INFLUXDB_ENABLED = False
    INFLUXDB_HOST = '10.169.17.25'
    INFLUXDB_PORT = 8086
    INFLUXDB_TOKEN = 'tKwtme7RYKb3c9LmBxxVKhWv-MF9TT9XTXFdlm5Q6eU6Q1RS0Ywjk2ND8a1S_CQUcpaNeEHWJwk9NQFnzJZa1g== '
    INFLUXDB_ORG = "IMRC"
    INFLUXDB_BUCKET = "HanDan"
    INFLUXDB_URL = "http://" + INFLUXDB_HOST + ":" + str(INFLUXDB_PORT)
    # INFLUXDB_DATABASE = "phm"

    KAFKA_ENABLED = False
    system_name = "qm"
    # system_name = "mc"
    kafka_topic = "qm_data"
    healthindex = 1
    kafka_bootstrap_servers = ["10.172.27.80:9092", "10.172.27.80:19092", "10.172.27.80:29092"]
    kafka_topic2 = 'alarm_data'
    device_a_tag_kafka_nodeId = {
        "A0102010003180423": "17",
        "A0102010003180424": "18",
        "A0102010003180426": "19",
        "A0102010003180422": "20",
        "A0102010003180425": "21"
    }

    iot_server_ip = "10.79.223.96"

    # 配置redis
    # 项目上线以后，这个地址就会被替换成真实IP地址，mysql也是
    REDIS_ENABLED = False
    REDIS_HOST = '10.141.6.103'
    REDIS_PORT = 6379
    REDIS_PASSWORD = ''
    # REDIS_PASSWORD = 'k+5p2K{nZf'
    REDIS_POLL = 10

    # 数据库地址配置
    # #mysql驱动
    # SQLALCHEMY_DATABASE_URI = "mysql+pymysql://root:Midea@2023@10.141.1.219:3307/qm"
    # sqlite3驱动
    # SQLALCHEMY_DATABASE_URI = "sqlite://../database/db.sqlite3"
    SQLALCHEMY_DATABASE_URI = 'sqlite:///' + os.path.join(basedir, 'database/db.sqlite3')
    # 动态追踪修改设置，如未设置只会提示警告
    SQLALCHEMY_TRACK_MODIFICATIONS = True
    # 查询时会显示原始SQL语句
    # SQLALCHEMY_ECHO = False
    # # 数据库连接池的大小
    # SQLALCHEMY_POOL_SIZE = 10
    # # 指定数据库连接池的超时时间
    # SQLALCHEMY_POOL_TIMEOUT = 10
    # # 控制在连接池达到最大值后可以创建的连接数。当这些额外的 连接回收到连接池后将会被断开和抛弃。
    # SQLALCHEMY_MAX_OVERFLOW = 2

    # rabbitmq参数配置
    RABBITUSER = "user"
    RABBITPASSWORD = "password"
    RABBITHOST = "your ip"
    RABBITPORT = 5372

    # API_ENABLED = True
    API_ENABLED = False
    API_TAG = "API_KAFKA"

class TestingConfig(Config):
    """项目配置核心类"""
    DEBUG = True
    TESTING = True
    # 配置日志
    # LOG_LEVEL = "DEBUG"
    LOG_LEVEL = "INFO"
    LOG_FILE = "/apps/data_acquisition/Logs/"

    # 配置MQTT
    # 项目上线以后，这个地址就会被替换成真实IP地址，
    MQTT_ENABLED = False
    MQTT_HOST = 'your host'
    MQTT_PORT = 1883
    MQTT_PASSWORD = 'your password'
    MQTT_POLL = 10

    # 配置INFLUXDB
    # 项目上线以后，这个地址就会被替换成真实IP地址
    INFLUXDB_ENABLED = False
    INFLUXDB_HOST = 'http://localhost'
    INFLUXDB_PORT = 64151
    INFLUXDB_TOKEN = '32dsfa'
    INFLUXDB_ORG = 10
    INFLUXDB_BUCKET = 2232

    # 配置redis
    # 项目上线以后，这个地址就会被替换成真实IP地址
    REDIS_ENABLED = False
    REDIS_HOST = 'your host'
    REDIS_PORT = 64151
    REDIS_PASSWORD = 'your password'
    REDIS_POLL = 10

    # 数据库地址配置
    # #mysql驱动
    # SQLALCHEMY_DATABASE_URI = "mysql+pymysql://root:Midea@2023@10.141.1.219:3307/qm"
    # sqlite3驱动
    SQLALCHEMY_DATABASE_URI = "sqlite://../database/db.sqlite3"
    # SQLALCHEMY_DATABASE_URI = 'sqlite:///' + os.path.join(basedir, 'database/db.sqlite3')
    # 动态追踪修改设置，如未设置只会提示警告
    SQLALCHEMY_TRACK_MODIFICATIONS = True
    # 查询时会显示原始SQL语句
    SQLALCHEMY_ECHO = False
    # 数据库连接池的大小
    SQLALCHEMY_POOL_SIZE = 10
    # 指定数据库连接池的超时时间
    SQLALCHEMY_POOL_TIMEOUT = 10
    # 控制在连接池达到最大值后可以创建的连接数。当这些额外的 连接回收到连接池后将会被断开和抛弃。
    SQLALCHEMY_MAX_OVERFLOW = 2

    # rabbitmq参数配置
    RABBITUSER = "user"
    RABBITPASSWORD = "password"
    RABBITHOST = "your ip"
    RABBITPORT = 5372

    # sqlite3 数据库路径
    SQLITE_DB_ENABLED = True
    SQLITE_DB_PATH = os.path.join(basedir, 'database/db.sqlite3')

    # art 设备IP
    ART_ENABLED = True
    ART_IP = ['10.141.1.222']


class ProductionConfig(Config):
    DEBUG = False


config_by_name = dict(
    dev=DevelopmentConfig,
    test=TestingConfig,
    prod=ProductionConfig
)
key = Config.SECRET_KEY
