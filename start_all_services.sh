#!/bin/bash
# 完整的服务启动脚本

echo "=== 停止现有服务 ==="
pkill -f "python3.*manage.py runserver" 2>/dev/null
pkill -f "celery.*worker" 2>/dev/null
pkill -f "modbus_mock_server" 2>/dev/null
sleep 3

echo "=== 启动Mock Modbus设备 (端口5020, 5021) ==="
python3 /mnt/c/Users/dongl/Downloads/0907/0907/edge_iot_v2/mock/modbus_mock_server.py --host 127.0.0.1 --port 5020 > /tmp/mock_5020.log 2>&1 &
python3 /mnt/c/Users/dongl/Downloads/0907/0907/edge_iot_v2/mock/modbus_mock_server.py --host 127.0.0.1 --port 5021 > /tmp/mock_5021.log 2>&1 &
sleep 2

echo "=== 启动Django ==="
PYTHONPATH=/mnt/c/Users/dongl/Downloads/0907/0907/edge_iot_v2/backend \
DJANGO_SETTINGS_MODULE=control_plane.settings \
python3 /mnt/c/Users/dongl/Downloads/0907/0907/edge_iot_v2/backend/manage.py runserver > /tmp/django.log 2>&1 &
sleep 5

echo "=== 启动Celery ==="
cd /mnt/c/Users/dongl/Downloads/0907/0907/edge_iot_v2/backend && \
DJANGO_SETTINGS_MODULE=control_plane.settings \
celery -A control_plane worker -l info --pool=solo > /tmp/celery.log 2>&1 &

echo ""
echo "=== 等待服务启动 ==="
sleep 5

echo ""
echo "=== 服务状态 ==="
echo "Mock设备 (5020):"
pgrep -af "modbus_mock_server.*5020" || echo "  未运行"

echo "Mock设备 (5021):"
pgrep -af "modbus_mock_server.*5021" || echo "  未运行"

echo "Django:"
pgrep -af "manage.py runserver" | head -1 || echo "  未运行"

echo "Celery:"
pgrep -af "celery.*worker" | head -1 || echo "  未运行"

echo ""
echo "=== 日志文件 ==="
echo "Mock 5020: tail -f /tmp/mock_5020.log"
echo "Mock 5021: tail -f /tmp/mock_5021.log"
echo "Django:    tail -f /tmp/django.log"
echo "Celery:    tail -f /tmp/celery.log"

echo ""
echo "=== 远程InfluxDB配置 ==="
echo "URL: http://123.207.247.44:8086"
echo "Org: edge-iot"
echo "Bucket: iot-data"

echo ""
echo "=== 所有服务已启动 ==="
