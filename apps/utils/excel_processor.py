import pandas as pd


def process_excel_data(df):
    """处理Excel数据,构建设备配置字典"""
    device_data_addresses = {}

    # 按protocol_type, source_ip, source_port分组,移除device_a_tag
    group_cols = ['protocol_type', 'source_ip', 'source_port']
    grouped = df.groupby(group_cols)

    for name, group in grouped:
        if not name[0]:  # protocol_type为空
            continue

        # 构建设备标识,不再使用device_a_tag
        port_address = f"{name[0]}{name[1]} : {name[2]}"

        # 初始化设备配置
        device_data_addresses[port_address] = {
            'protocol_type': name[0],
            'source_ip': name[1],
            'source_port': name[2],
            'source_slave_addr': group['source_slave_addr'].iloc[0],
            'fs': group['fs'].iloc[0],
        }

        # 添加数据项配置
        for _, row in group.iterrows():
            device_data_addresses[port_address][row.get('en_name')] = {
                'device_a_tag': row.get('device_a_tag'),
                'device_name':row.get('device_name'),
                'en_name': row.get('en_name'),
                'cn_name': row.get('cn_name'),
                'source_addr': row.get('source_addr'),
                'part_name': row.get('part_name'),
                'input_data_maximum': row.get('input_data_maximum'),
                'input_data_minimum': row.get('input_data_minimum'),
                'output_data_minimum': row.get('output_data_minimum'),
                'output_data_maximum': row.get('output_data_maximum'),
                'unit': row.get('unit'),
                'data_source': row.get('data_source'),
                'num': row.get('num'),
                'type': row.get('type'),
                'coefficient': row.get('coefficient'),
                'precision': row.get('precision'),
                'kafka_position': row.get('kafka_position'),
                'to_kafka': row.get('to_kafka')
            }

    return device_data_addresses 