#!/bin/bash
# 测试运行脚本

set -e

echo "========================================="
echo "  Edge IoT Backend Test Suite"
echo "========================================="
echo ""

# 检查是否在backend目录
if [ ! -f "manage.py" ]; then
    echo "错误: 请在backend目录下运行此脚本"
    exit 1
fi

# 安装测试依赖
echo "📦 安装测试依赖..."
pip install -q -r tests/requirements-test.txt

# 运行测试
echo ""
echo "🧪 运行测试套件..."
echo ""

# 根据参数运行不同测试
case "$1" in
    "quick")
        echo "⚡ 快速测试 (仅单元测试)..."
        pytest tests/test_protocols.py tests/test_storage.py -v
        ;;
    "coverage")
        echo "📊 运行测试并生成覆盖率报告..."
        pytest --cov=acquisition --cov=storage --cov-report=html --cov-report=term
        echo ""
        echo "✅ 覆盖率报告已生成: htmlcov/index.html"
        ;;
    "integration")
        echo "🔗 仅运行集成测试..."
        pytest tests/test_integration.py -v
        ;;
    "verbose")
        echo "📝 详细模式..."
        pytest -vv --tb=long
        ;;
    *)
        echo "🎯 运行所有测试..."
        pytest -v
        ;;
esac

# 测试结果
if [ $? -eq 0 ]; then
    echo ""
    echo "========================================="
    echo "  ✅ 所有测试通过!"
    echo "========================================="
    exit 0
else
    echo ""
    echo "========================================="
    echo "  ❌ 测试失败"
    echo "========================================="
    exit 1
fi
