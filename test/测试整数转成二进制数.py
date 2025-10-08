num = 255  # 示例整数

# 使用 bin() 函数获取二进制字符串，并去掉 '0b' 前缀
binary_str = bin(num)[2:]

# 使用 zfill() 函数填充前导零，直到长度为16
binary_str_16bit = binary_str.zfill(16)

print("16位二进制表示是:", binary_str_16bit)