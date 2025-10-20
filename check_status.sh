#!/bin/bash
# 检查系统状态

echo "========================================="
echo "系统状态检查"
echo "========================================="

# 检查Django
echo -e "\n[Django]"
DJANGO_PID=$(ps aux | grep "manage.py runserver" | grep -v grep | head -1 | awk '{print $2}')
if [ -n "$DJANGO_PID" ]; then
    echo "  ✓ 运行中 (PID: $DJANGO_PID)"
    # 检查环境变量
    INFLUX_HOST=$(cat /proc/$DJANGO_PID/environ | tr '\0' '\n' | grep INFLUXDB_HOST | cut -d= -f2)
    INFLUX_URL=$(cat /proc/$DJANGO_PID/environ | tr '\0' '\n' | grep INFLUXDB_URL | cut -d= -f2)
    echo "    INFLUXDB_HOST=$INFLUX_HOST"
    echo "    INFLUXDB_URL=$INFLUX_URL"
else
    echo "  ✗ 未运行"
fi

# 检查Celery
echo -e "\n[Celery Worker]"
CELERY_PID=$(ps aux | grep "celery.*worker" | grep -v grep | awk '{print $2}')
if [ -n "$CELERY_PID" ]; then
    echo "  ✓ 运行中 (PID: $CELERY_PID)"
else
    echo "  ✗ 未运行"
fi

# 检查Mock设备
echo -e "\n[Mock Modbus设备]"
MOCK_PIDS=$(ps aux | grep "modbus_mock_server" | grep -v grep | awk '{print $2}')
if [ -n "$MOCK_PIDS" ]; then
    echo "  ✓ 运行中"
    ps aux | grep "modbus_mock_server" | grep -v grep | awk '{print "    PID "$2" - Port: "$NF}'
else
    echo "  ✗ 未运行"
fi

# 检查采集会话
echo -e "\n[采集会话]"
python3 <<'PYEOF'
import os, sys, django
sys.path.insert(0, '/mnt/c/Users/dongl/Downloads/0907/0907/edge_iot_v2/backend')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'control_plane.settings')
django.setup()

from acquisition.models import AcquisitionSession
sessions = AcquisitionSession.objects.filter(status='running')
if sessions.exists():
    print(f"  ✓ {sessions.count()} 个会话运行中")
    for s in sessions:
        print(f"    Session {s.id}: {s.task.name}")
else:
    print("  ✗ 无运行中的会话")
PYEOF

# 检查InfluxDB数据
echo -e "\n[InfluxDB数据]"
python3 <<'PYEOF'
from influxdb_client import InfluxDBClient

INFLUXDB_URL = "http://123.207.247.44:8086"
INFLUXDB_TOKEN = "ZoXRo0iE2d5rqvKPe_YNk3dus-xUPEcgPa5-e5LtUqB2OT6_yb5yyhJkes4J9bvf3FiWygVb6H8k3DSBIUwQaQ=="
INFLUXDB_ORG = "edge-iot"
INFLUXDB_BUCKET = "iot-data"

try:
    client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
    query_api = client.query_api()

    query = f'''
    from(bucket:"{INFLUXDB_BUCKET}")
      |> range(start: -5m)
      |> filter(fn: (r) => r["_measurement"] =~ /modbustcp/)
      |> group()
      |> count()
    '''

    result = query_api.query(query, org=INFLUXDB_ORG)
    if result and len(result) > 0:
        count = sum(record.get_value() for table in result for record in table.records)
        print(f"  ✓ 最近5分钟: {count} 条记录")
    else:
        print("  ✗ 最近5分钟: 0 条记录")
    client.close()
except Exception as e:
    print(f"  ✗ 连接失败: {e}")
PYEOF

echo -e "\n========================================="
