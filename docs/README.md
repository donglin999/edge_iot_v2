# 边缘IoT数据采集系统文档

## 目录结构

- [系统架构](SYSTEM_ARCHITECTURE.md) - 系统整体架构与核心模块说明
- [快速开始](QUICKSTART.md) - 环境配置与启动指南
- [API参考](API_REFERENCE.md) - 后端API接口文档
- [部署指南](DEPLOYMENT.md) - Docker部署与运维手册
- [开发指南](DEVELOPMENT.md) - 本地开发与测试指南

## 系统概览

边缘IoT数据采集系统是一个基于Django + Celery + InfluxDB的工业数据采集平台，支持Modbus TCP、MELSEC A1E、MQTT等多种工业协议。

### 核心特性

- **持久连接**: 保持设备连接，避免频繁建立/断开，提高采集效率
- **批量上传**: 数据批量写入InfluxDB，优化存储性能
- **健康监控**: 实时监控设备状态，自动重连，超时检测
- **同步验证**: 5秒快速启动验证，无中间状态
- **协议可扩展**: 支持多种工业协议，易于扩展新协议
- **Web可视化**: 实时查看采集状态和历史数据

### 技术栈

| 层级 | 技术 |
| --- | --- |
| 后端框架 | Django 4.2 + Django REST Framework |
| 任务队列 | Celery + Redis |
| 时序数据库 | InfluxDB 2.x |
| 关系数据库 | SQLite3 |
| 前端框架 | React + TypeScript + Vite |
| 通信协议 | WebSocket (实时数据) |
