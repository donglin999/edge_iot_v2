import struct

def create_modbus_read_request(slave_id, function_code, start_address, quantity):
    """
    创建一个Modbus RTU读取数据请求的字节序列。

    :param slave_id: 从站地址（0-247）
    :param function_code: 功能码（3或4，分别代表读保持寄存器和读输入寄存器）
    :param start_address: 起始地址（16位整数）
    :param quantity: 寄存器个数（16位整数）
    :return: Modbus RTU读取数据请求的字节序列
    """
    # 构建请求报文：从站地址 + 功能码 + 起始地址（两个字节）+ 寄存器个数（两个字节）
    request = struct.pack('>BBHH', slave_id, function_code, start_address, quantity)

    # 计算CRC校验值
    crc = modbus_crc(request)

    # 将CRC添加到请求报文中
    request += struct.pack('>H', crc)

    return request

def modbus_crc(data):
    """
    计算Modbus CRC校验值。

    :param data: 待计算CRC的字节序列
    :return: CRC校验值（16位整数）
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc >>= 1
                crc ^= 0xA001
            else:
                crc >>= 1
    return crc

# 示例调用
request = create_modbus_read_request(12, 3, 500, 4)
print("Modbus RTU Read Request:", request)
# 获取CRC高位和低位字节
crc_high = request[-2]
crc_low = request[-1]
# 创建一个新的字节序列，其中CRC位置被交换
new_request = request[:-2] + bytes([crc_low, crc_high])
print("request:", request)
print("New request with swapped CRC:", new_request)
