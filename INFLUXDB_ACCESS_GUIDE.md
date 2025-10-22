# InfluxDB 访问指南

## 🔑 登录信息

### Web界面登录
- **访问地址**: http://localhost:8086
- **用户名**: admin  
- **密码**: admin123456

### API访问
- **Token**: my-super-secret-auth-token
- **组织**: edge-iot
- **存储桶**: iot-data

## 🌐 访问方式

### 1. Web界面访问
1. 打开浏览器访问: http://localhost:8086
2. 输入用户名: `admin`
3. 输入密码: `admin123456`
4. 点击登录

### 2. API访问示例
```bash
# 查询数据
curl -X POST "http://localhost:8086/api/v2/query?org=edge-iot" \
  -H "Authorization: Token my-super-secret-auth-token" \
  -H "Content-Type: application/vnd.flux" \
  -d 'from(bucket:"iot-data") |> range(start:-1h)'

# 写入数据
curl -X POST "http://localhost:8086/api/v2/write?org=edge-iot&bucket=iot-data" \
  -H "Authorization: Token my-super-secret-auth-token" \
  -H "Content-Type: text/plain; charset=utf-8" \
  --data-raw 'temperature,location=room1 value=23.5'
```

### 3. CLI访问
```bash
# 进入容器
docker exec -it influxdb-iot bash

# 使用CLI查询
influx query 'from(bucket:"iot-data") |> range(start:-1h)' \
  --token my-super-secret-auth-token --org edge-iot
```

## 🔧 故障排除

### 如果Web界面无法登录：

1. **重置密码**:
```bash
docker exec influxdb-iot influx user password \
  --name admin \
  --token my-super-secret-auth-token \
  --password admin123456
```

2. **检查用户状态**:
```bash
docker exec influxdb-iot influx user list
docker exec influxdb-iot influx auth list
```

3. **重新创建InfluxDB容器**:
```bash
# 停止并删除容器
docker stop influxdb-iot && docker rm influxdb-iot

# 删除数据卷（注意：这会丢失所有数据）
docker volume rm influxdb-data influxdb-config

# 重新启动
docker-compose up -d influxdb
```

## 📊 常用操作

### 查看组织和存储桶
```bash
docker exec influxdb-iot influx org list
docker exec influxdb-iot influx bucket list
```

### 创建新的Token
```bash
docker exec influxdb-iot influx auth create \
  --org edge-iot \
  --all-access \
  --description "New API Token"
```

### 查看系统状态
```bash
docker exec influxdb-iot influx ping
curl http://localhost:8086/health
```

## 🔒 安全建议

1. **更改默认密码**: 在生产环境中务必更改默认密码
2. **限制Token权限**: 为不同用途创建具有最小权限的Token
3. **网络安全**: 在生产环境中配置防火墙规则
4. **备份数据**: 定期备份InfluxDB数据

## 📝 Python客户端示例

```python
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

# 连接配置
url = "http://localhost:8086"
token = "my-super-secret-auth-token"
org = "edge-iot"
bucket = "iot-data"

# 创建客户端
client = InfluxDBClient(url=url, token=token, org=org)

# 写入数据
write_api = client.write_api(write_options=SYNCHRONOUS)
point = Point("temperature").tag("location", "room1").field("value", 23.5)
write_api.write(bucket=bucket, record=point)

# 查询数据
query_api = client.query_api()
query = f'from(bucket:"{bucket}") |> range(start:-1h)'
result = query_api.query(query)

client.close()
```






