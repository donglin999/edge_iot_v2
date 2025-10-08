# 内容保持不变,只是移动位置 

def get_kafka_config(device_data_addresses):
    """获取需要发送到kafka的数据配置"""
    kafka_config = {}
    for device_addr, device_info in device_data_addresses.items():
        device_a_tag = device_info['device_a_tag']
        if device_a_tag not in kafka_config:
            kafka_config[device_a_tag] = {
                'device_name': device_info['device_name'],
                'fields': []
            }
        
        # 遍历设备的所有数据项
        for field, field_info in device_info.items():
            if isinstance(field_info, dict) and field_info.get('to_kafka') == 1:
                kafka_config[device_a_tag]['fields'].append({
                    'field_name': field,
                    'kafka_position': field_info.get('kafka_position', ''),
                    'cn_name': field_info.get('cn_name', '')
                })
                
    return kafka_config 