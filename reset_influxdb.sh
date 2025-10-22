#!/bin/bash
# InfluxDB 重置脚本

echo "🔄 重置InfluxDB..."

# 停止并删除容器
docker stop influxdb-iot 2>/dev/null || true
docker rm influxdb-iot 2>/dev/null || true

# 删除数据卷
docker volume rm influxdb-data influxdb-config 2>/dev/null || true

# 重新创建并启动
docker-compose up -d influxdb

echo "⏳ 等待InfluxDB启动..."
sleep 30

echo "✅ InfluxDB重置完成！"
echo ""
echo "🌐 现在请访问: http://localhost:8086"
echo "📝 首次访问会要求设置管理员账号"
echo "💡 建议设置:"
echo "   用户名: admin"
echo "   密码: admin123456"
echo "   组织: edge-iot"
echo "   存储桶: iot-data"






