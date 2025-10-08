import struct

def parse_modbus_rtu_response(response):
    """
    解析Modbus RTU响应报文，提取数据。

    :param response: Modbus RTU响应报文（字节序列）
    :return: 一个字典，包含从站地址、功能码和读取到的数据
    """
    # 检查响应长度是否至少为7字节（最小有效长度）
    if len(response) < 7:
        raise ValueError("响应太短，无法解析")

    # 解析从站地址和功能码
    slave_id = response[0]
    function_code = response[1]

    # 根据功能码判断数据格式
    if function_code == 3 or function_code == 4:  # 读保持寄存器或读输入寄存器
        # 跳过前两个字节（从站地址和功能码），然后是字节数字节
        data_length = response[2]
        # 紧接着是实际数据，根据字节数字节确定数据长度
        data_start = 3
        data_end = data_start + data_length
        data = response[data_start:data_end]

        # 如果数据长度是偶数，表示每个寄存器占用两个字节，需要转换为整数
        if data_length % 2 == 0:
            # 将字节序列转换为整数列表
            values = []
            for i in range(0, data_length, 2):
                value = struct.unpack('>H', data[i:i+2])[0]
                values.append(value)
            return {'slave_id': slave_id, 'function_code': function_code, 'data': values}
        else:
            # 如果数据长度不是偶数，可能有错误或非标准实现
            raise ValueError("数据长度不是偶数，无法正确解析")
    else:
        # 其他功能码可能有不同的解析方式
        raise ValueError("不支持的功能码")

# 示例调用
response = b'\x0c\x03\x04\x00\x00\x95\x85'
parsed_data = parse_modbus_rtu_response(response)
print(parsed_data)
