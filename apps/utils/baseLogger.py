#!/usr/bin/env python
# -*- encoding: utf-8 -*-
'''
@File    :   baseLogger.py
@Time    :   2023/04/27 16:24:59
@Author  :   Jason Jiangfeng 
@Version :   1.0
@Contact :   jiangfeng24@midea.com
@Desc    :   base log class
'''

# here put the import lib
import logging
import os
import time
from datetime import datetime

del_files = 7

log_path = "/app/logs/"
# log_path = "d:/Users/ex_wuxx18/Desktop/midea_project/project-data-acquisition/"


def delete_files(folder_path):
    # 遍历文件夹中的所有文件和子文件夹
    for filename in os.listdir(folder_path):
        file_path = os.path.join(folder_path, filename)
        if os.path.isfile(file_path):
            # 获取文件的创建时间
            create_time = datetime.fromtimestamp(os.path.getmtime(file_path))
            # 计算文件创建时间距离现在的时间差
            time_diff = datetime.now() - create_time
            # 如果时间差超过1周，则删除该文件
            if time_diff.days > del_files:
                os.remove(file_path)
        elif os.path.isdir(file_path):
            # 递归调用自身，继续遍历子文件夹中的所有文件和子文件夹
            delete_files(file_path)
    # 删除空子文件夹
    if not os.listdir(folder_path):
        os.rmdir(folder_path)
        
class Log:
    def __init__(self, name="default"):
        self.logger = logging.getLogger()  # 创建logger
        self.logger.setLevel(logging.INFO)  # 日志root等级
        # 自动确保name以/结尾
        if not name.endswith("/"):
            name += "/"
        self.log_path = os.path.join(log_path, name)  # 日志目录
        # 日志内容格式
        self.formatter = logging.Formatter("%(asctime)s - %(filename)s[line:%(lineno)d] - %(levelname)s: %(message)s")
        if not os.path.exists(self.log_path):  # 目录不存在就创建
            os.makedirs(self.log_path)

    def printLog(self, log_type, log_content):  # 输出日志
        logTime = time.strftime('%Y%m%d%H', time.localtime(time.time()))  # 当前时间到小时
        log_file = self.log_path + logTime + '.log'  # 文件名
        if not os.path.exists(log_file):  # 日志文件不存在就创建
            fd = open(log_file, mode="w", encoding="utf_8_sig")
            fd.close()
        
        try:
            delete_files(log_path)
        except Exception as e:
            pass

        handler = logging.FileHandler(log_file, mode='a')
        handler.setLevel(logging.DEBUG)  # handler的日志等级
        handler.setFormatter(self.formatter)  # 设置日志格式
        self.logger.addHandler(handler)  # 添加handler
        if log_type == 0:
            self.logger.info(log_content)
        elif log_type == 1:
            self.logger.warning(log_content)
        elif log_type == 2:
            self.logger.error(log_content)
        else:
            self.logger.critical(log_content)
        self.logger.removeHandler(handler)  # 记得删除handler防止重复打印

    def printInfo(self, log_content):
        self.printLog(0, log_content)

    def printWarning(self, log_content):
        self.printLog(1, log_content)

    def printError(self, log_content):
        self.printLog(2, log_content)

    def printCritical(self, log_content):
        self.printLog(3, log_content)


if __name__ == '__main__':
    Log = Log()
    Log.printInfo('111')
    Log.printWarning('222')
    Log.printError('333')
