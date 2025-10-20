#!/usr/bin/env python3
"""测试Modbus连接"""
from pymodbus.client import ModbusTcpClient

print("=" * 60)
print("Modbus TCP 连接测试")
print("=" * 60)

# 连接参数
HOST = "127.0.0.1"
PORT = 5020

print(f"\n连接到 {HOST}:{PORT}...")
client = ModbusTcpClient(HOST, port=PORT, timeout=5)

if not client.connect():
    print("✗ 连接失败!")
    exit(1)

print("✓ 连接成功!")

# 测试读取不同的寄存器地址
test_addresses = [
    (0, 10, "0-9 (原始地址)"),
    (40001, 7, "40001-40007 (系统尝试的地址)"),
    (100, 5, "100-104"),
]

for start_addr, count, desc in test_addresses:
    print(f"\n测试读取 {desc}:")
    try:
        # 读取保持寄存器 (function code 3)
        result = client.read_holding_registers(address=start_addr, count=count, slave=1)

        if result.isError():
            print(f"  ✗ 读取失败: {result}")
        else:
            print(f"  ✓ 读取成功! 值: {result.registers[:min(5, len(result.registers))]}...")  # 只显示前5个
    except Exception as e:
        print(f"  ✗ 异常: {e}")

client.close()
print("\n" + "=" * 60)
print("测试完成!")
print("=" * 60)
