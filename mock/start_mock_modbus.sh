#!/bin/bash
# 启动 Mock Modbus TCP 服务器
# 用于模拟 Modbus 设备进行测试

# 默认配置
HOST="127.0.0.1"
PORT=502

# 检查端口是否被占用
if lsof -Pi :$PORT -sTCP:LISTEN -t >/dev/null 2>&1 ; then
    echo "警告: 端口 $PORT 已被占用"
    echo "尝试使用其他端口..."
    PORT=5020
    echo "使用端口: $PORT"
fi

# 启动服务器
echo "=========================================="
echo "启动 Mock Modbus TCP 服务器"
echo "Host: $HOST"
echo "Port: $PORT"
echo "=========================================="
echo ""
echo "提示: 按 Ctrl+C 停止服务器"
echo ""

# 如果端口是 502，需要 sudo 权限
if [ "$PORT" -eq 502 ]; then
    echo "注意: 端口 502 需要 root 权限"
    sudo python3 modbus_mock_server.py --host $HOST --port $PORT
else
    python3 modbus_mock_server.py --host $HOST --port $PORT
fi
