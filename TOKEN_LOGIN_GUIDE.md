# 🔑 InfluxDB Token 登录完整指南

## 📋 Token 信息
- **Token**: `my-super-secret-auth-token`
- **组织**: `edge-iot`
- **用户**: `admin`
- **访问地址**: http://localhost:8086

## 🌐 Web界面Token登录步骤

### 步骤1：访问InfluxDB Web界面
打开浏览器，访问：
```
http://localhost:8086
```

### 步骤2：选择Token登录方式
在登录页面寻找以下选项之一：
- ✅ "Use Token" 按钮
- ✅ "API Token" 标签页  
- ✅ "Sign in with Token" 链接
- ✅ "Token Authentication" 选项

### 步骤3：输入Token信息
```
Token: my-super-secret-auth-token
Organization: edge-iot
```

### 步骤4：点击登录
点击 "Sign In" 或 "Continue" 按钮

## 🔧 备选登录方式

### 方式1：直接URL访问
尝试以下URL（某些版本支持）：
```
http://localhost:8086/?token=my-super-secret-auth-token&org=edge-iot
```

### 方式2：如果没有Token选项
如果Web界面只显示用户名/密码登录：
1. 用户名: `admin`
2. 密码: `admin123456`

## 🛠 API 访问示例

### 查询数据
```bash
curl -X POST "http://localhost:8086/api/v2/query?org=edge-iot" \
  -H "Authorization: Token my-super-secret-auth-token" \
  -H "Content-Type: application/vnd.flux" \
  -d 'from(bucket:"iot-data") |> range(start:-1h) |> limit(n:10)'
```

### 写入数据
```bash
curl -X POST "http://localhost:8086/api/v2/write?org=edge-iot&bucket=iot-data" \
  -H "Authorization: Token my-super-secret-auth-token" \
  -H "Content-Type: text/plain; charset=utf-8" \
  --data-raw 'temperature,location=room1 value=23.5'
```

### 获取用户信息
```bash
curl "http://localhost:8086/api/v2/me" \
  -H "Authorization: Token my-super-secret-auth-token"
```

## 🐍 Python 客户端示例

```python
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

# 配置信息
url = "http://localhost:8086"
token = "my-super-secret-auth-token"
org = "edge-iot"
bucket = "iot-data"

# 创建客户端
client = InfluxDBClient(url=url, token=token, org=org)

try:
    # 测试连接
    health = client.health()
    print(f"InfluxDB 状态: {health.status}")
    
    # 写入数据
    write_api = client.write_api(write_options=SYNCHRONOUS)
    point = Point("temperature") \
        .tag("location", "room1") \
        .field("value", 23.5)
    write_api.write(bucket=bucket, record=point)
    
    # 查询数据
    query_api = client.query_api()
    query = f'''
    from(bucket:"{bucket}")
    |> range(start:-1h)
    |> filter(fn:(r) => r._measurement == "temperature")
    '''
    result = query_api.query(query)
    
    for table in result:
        for record in table.records:
            print(f"时间: {record.get_time()}, 值: {record.get_value()}")
            
finally:
    client.close()
```

## 🔍 故障排除

### 问题1: Web界面无Token登录选项
**解决方案**: 
- 刷新页面多次
- 清除浏览器缓存
- 尝试不同浏览器
- 使用用户名密码登录

### 问题2: Token无效
**验证Token**:
```bash
docker exec influxdb-iot influx auth list
```

**重新创建Token**:
```bash
docker exec influxdb-iot influx auth create \
  --org edge-iot \
  --all-access \
  --description "新的API Token"
```

### 问题3: 网络连接问题
**检查服务状态**:
```bash
docker ps | grep influx
curl -I http://localhost:8086
```

### 问题4: 完全重置
如果所有方法都不行，使用重置脚本：
```bash
./reset_influxdb.sh
```

## 📱 移动端/远程访问

如果需要从其他设备访问，将 `localhost` 替换为服务器IP：
```
http://YOUR_SERVER_IP:8086
```

记得在防火墙中开放8086端口。

## 🔐 安全提示

1. **生产环境**: 更改默认Token和密码
2. **网络安全**: 配置HTTPS和防火墙
3. **权限控制**: 为不同用途创建专用Token
4. **定期轮换**: 定期更新Token和密码





