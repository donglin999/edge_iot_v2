import csv

# 给定的数据
data = {
    "device_map": [
        {
            "device_a_tag":"A0201010001160230",
            "device_name":"1#"
        },
        {
            "device_a_tag":"A0201010001160232",
            "device_name":"2#"
        },
        {
            "device_a_tag":"A0201010001160234",
            "device_name":"3#"
        },
        {
            "device_a_tag":"A0201010001160236",
            "device_name":"4#"
        },
        {
            "device_a_tag":"A0201010001160186",
            "device_name":"5#"
        },
        {
            "device_a_tag":"A0201010001160239",
            "device_name":"6#"
        },
        {
            "device_a_tag":"A0201010001160252",
            "device_name":"7#"
        },
        {
            "device_a_tag":"A0201010001160241",
            "device_name":"8#"
        },
        {
            "device_a_tag":"A0201010001160242",
            "device_name":"9#"
        },
        {
            "device_a_tag":"A0201010001160185",
            "device_name":"11#"
        },
        {
            "device_a_tag":"A0201010001160255",
            "device_name":"12#"
        },
        {
            "device_a_tag":"A0201010001160261",
            "device_name":"13#"
        },
        {
            "device_a_tag":"A0201010001160231",
            "device_name":"14#"
        },
        {
            "device_a_tag":"A0201010001160244",
            "device_name":"15#"
        },
        {
            "device_a_tag":"A0201010001160233",
            "device_name":"16#"
        },
        {
            "device_a_tag":"A0201010001160235",
            "device_name":"17#"
        },
        {
            "device_a_tag":"A0201010001160237",
            "device_name":"18#"
        },
        {
            "device_a_tag":"A0201010001160238",
            "device_name":"19#"
        },
        {
            "device_a_tag":"A0201010001210241",
            "device_name":"21#"
        },
        {
            "device_a_tag":"A0201010001210242",
            "device_name":"22#"
        },
        {
            "device_a_tag":"A0201010001210243",
            "device_name":"23#"
        },
        {
            "device_a_tag":"A0201010001210244",
            "device_name":"24#"
        },
        {
            "device_a_tag":"A0201010001210245",
            "device_name":"25#"
        },
        {
            "device_a_tag":"A0201010001210246",
            "device_name":"26#"
        },
        {
            "device_a_tag":"A0201010001210247",
            "device_name":"27#"
        },
        {
            "device_a_tag":"A0201010001210248",
            "device_name":"28#"
        },
        {
            "device_a_tag":"A0201010001210249",
            "device_name":"29#"
        },
        {
            "device_a_tag":"A0201010001210250",
            "device_name":"30#"
        }
    ]
}

# 定义CSV文件的列名
fieldnames = ["device_a_tag", "device_name"]

# 写入CSV文件
with open('device_map.csv', mode='w', newline='') as file:
    writer = csv.DictWriter(file, fieldnames=fieldnames)

    # 写入表头
    writer.writeheader()

    # 写入数据行
    for device in data["device_map"]:
        writer.writerow(device)

print("数据已成功写入device_map.csv文件中。")