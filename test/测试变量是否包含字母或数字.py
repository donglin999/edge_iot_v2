def contains_alpha_or_digit(s):
    # 检查是否包含字母
    contains_alpha = any(char.isalpha() for char in s)
    # 检查是否包含数字
    contains_digit = any(char.isdigit() for char in s)

    # 如果包含字母或数字，就打印消息
    if contains_alpha or contains_digit:
        print(f"{s}变量包含字母或数字")
    else:
        # 可选：如果不包含，也可以打印一条消息（这里省略）
        pass


# 测试
var1 = "hello123"
var2 = "!@#$%"
var3 = "abc"
var4 = "123456"
var5 = ""  # 空字符串

contains_alpha_or_digit(var1)  # 输出: 变量包含字母或数字
contains_alpha_or_digit(var2)  # 不输出（因为没有字母或数字）
contains_alpha_or_digit(var3)  # 输出: 变量包含字母或数字
contains_alpha_or_digit(var4)  # 输出: 变量包含字母或数字
contains_alpha_or_digit(var5)  # 不输出（因为是空字符串）