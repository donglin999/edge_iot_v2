#!/usr/bin/env python
# -*- encoding: utf-8 -*-

import ssl
import paho.mqtt.client as mqtt
from retry import retry
from settings import DevelopmentConfig
from apps.utils.baseLogger import Log

class MqttClient:
    def __init__(self, data_addresses):
        self.topics = []
        if data_addresses['protocol_type'] == 'MQTT_SIEMENS':
            self.ip = data_addresses['source_ip']
            self.port = data_addresses['source_port']
            self.topic = DevelopmentConfig().device_mqtt_topic_start[0] + data_addresses['device_a_tag'] + DevelopmentConfig().device_mqtt_topic_end[0]
        elif data_addresses['protocol_type'] == 'modbustcp_mqtt':
            self.ip = DevelopmentConfig().MQTT_HOST
            self.port = DevelopmentConfig().MQTT_PORT
            self.topic = DevelopmentConfig().MQTT_TOPIC
        else:
            self.ip = DevelopmentConfig().MQTT_HOST
            self.port = DevelopmentConfig().MQTT_PORT
            self.topic = DevelopmentConfig().device_mqtt_topic_start[0] + data_addresses['device_a_tag'] + DevelopmentConfig().device_mqtt_topic_end[0]
            self.topics.append(self.topic)

        self.username = DevelopmentConfig().MQTT_USERNAME
        self.passwd = DevelopmentConfig().MQTT_PASSWORD
        # print(f"DevelopmentConfig().device_mqtt_topic_start:{DevelopmentConfig().device_mqtt_topic_start}")
        # print(f"data_addresses['device_a_tag']:{data_addresses['device_a_tag']}")
        # print(f"DevelopmentConfig().device_mqtt_topic_end:{DevelopmentConfig().device_mqtt_topic_end}")
        print(f"self.topics:{self.topics}")
        self.data_addresses = data_addresses
        print(f"初始化MattClient完成, self.data_addresses:{self.data_addresses}")

    def connect(self):
        # print(f"self.ip:{self.ip},self.port:{self.port}")
        # print(f"self.username:{self.username}")
        # print(f"self.passwd:{self.passwd}")
        # print(f"self.topic:{self.topic}")
        client = mqtt.Client(protocol=mqtt.MQTTv5)
        # client = mqtt.Client()
        # print(f"创建对象完成：{client}")
        client.username_pw_set(self.username, self.passwd)
        context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
        client.tls_set_context(context)
        client.connect(self.ip, self.port)
        print(f"函数connect连接服务器{self.ip}{self.port}完成")
        client.subscribe(self.topic)
        return client

    def connect1(self):
        # print(f"self.ip:{self.ip},self.port:{self.port}")
        # print(f"self.username:{self.username}")
        # print(f"self.passwd:{self.passwd}")
        # print(f"self.topic:{self.topic}")
        client = mqtt.Client(protocol=mqtt.MQTTv5)
        # client = mqtt.Client()
        # print(f"创建对象完成：{client}")
        client.username_pw_set(self.username, self.passwd)
        context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
        client.tls_set_context(context)
        client.connect(self.ip, self.port)
        # print(f"连接服务器完成：{client}")
        # 订阅所有主题
        for topic in self.topics:
            client.subscribe(topic)
        return client

    def connect2(self):
        # print(f"self.ip:{self.ip},self.port:{self.port}")
        # print(f"self.username:{self.username}")
        # print(f"self.passwd:{self.passwd}")
        # print(f"self.topic:{self.topic}")
        # client = mqtt.Client(protocol=mqtt.MQTTv5)
        client = mqtt.Client()
        # print(f"创建对象完成：{client}")
        # client.username_pw_set(self.username, self.passwd)
        # context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
        # client.tls_set_context(context)
        client.connect(self.ip, self.port)
        print(f"函数connect2连接服务器{self.ip}{self.port}完成")
        client.subscribe(self.topic)
        return client

