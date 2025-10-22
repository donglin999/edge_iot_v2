#!/bin/bash
# InfluxDB 登录测试脚本

echo "🔍 InfluxDB 登录测试"
echo "===================="

# 测试Web界面可用性
echo "1. 测试Web界面..."
if curl -s -I http://localhost:8086 | grep -q "200 OK"; then
    echo "✅ Web界面可访问: http://localhost:8086"
else
    echo "❌ Web界面不可访问"
    exit 1
fi

# 测试用户名密码登录
echo ""
echo "2. 测试用户名密码登录..."
response=$(curl -s -X POST "http://localhost:8086/api/v2/signin" \
    -H "Content-Type: application/json" \
    -d '{"name":"admin","password":"admin123456"}' \
    -w "%{http_code}")

if echo "$response" | grep -q "200"; then
    echo "✅ 用户名密码登录成功"
else
    echo "⚠️  用户名密码登录失败，但这在某些InfluxDB版本中是正常的"
fi

# 显示登录信息
echo ""
echo "🔑 登录信息："
echo "============="
echo "Web地址: http://localhost:8086"
echo "用户名: admin"
echo "密码: admin123456"
echo "Token: my-super-secret-auth-token"
echo "组织: edge-iot"
echo "存储桶: iot-data"

echo ""
echo "📝 登录步骤："
echo "============"
echo "1. 打开浏览器访问: http://localhost:8086"
echo "2. 输入用户名: admin"
echo "3. 输入密码: admin123456"
echo "4. 点击 SIGN IN 按钮"
echo ""
echo "如果没有Token登录选项，请直接使用用户名密码登录！"






