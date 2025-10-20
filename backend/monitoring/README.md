# Monitoring Module (Plan C)

## 概述

此模块为方案C预留，用于实现高级监控和告警功能。

## 计划功能

### 1. 告警系统 (`alerts/`)
- **设备离线告警**: 监控设备连接状态，超时未响应时告警
- **数据异常检测**: 监控数据点，检测异常值（超出阈值、数据突变等）
- **采集失败告警**: 监控采集会话，连续失败时告警
- **存储失败告警**: 监控存储写入，失败率超过阈值时告警

### 2. 通知渠道 (`notifications/`)
- **邮件通知**: SMTP邮件发送
- **短信通知**: SMS网关集成
- **WebHook通知**: POST通知到指定URL
- **企业微信/钉钉**: 企业IM集成

### 3. 性能监控 (`performance/`)
- **采集性能统计**:
  - 采集周期准确度
  - 数据点采集速率
  - 网络延迟统计
- **系统资源监控**:
  - CPU/内存使用率
  - 磁盘空间
  - 网络带宽
- **数据库性能**:
  - 查询响应时间
  - 连接池状态

### 4. 数据质量监控 (`quality/`)
- **数据完整性**: 检测缺失数据
- **数据一致性**: 检测数据冲突
- **数据准确性**: 与已知值对比
- **质量报告**: 定期生成数据质量报告

### 5. 健康检查 (`health/`)
- **设备健康检查**: 定期ping设备
- **服务健康检查**: 检查Celery/Redis/InfluxDB状态
- **依赖项检查**: 检查外部API可用性
- **健康度评分**: 综合健康状态评分

## 数据模型设计

```python
# monitoring/models.py

class AlertRule(models.Model):
    """告警规则"""
    name = models.CharField(max_length=255)
    rule_type = models.CharField(choices=[
        ('device_offline', '设备离线'),
        ('data_anomaly', '数据异常'),
        ('acquisition_failure', '采集失败'),
        ('storage_failure', '存储失败'),
    ])
    condition = models.JSONField()  # 告警条件
    threshold = models.FloatField()  # 阈值
    severity = models.CharField(choices=[
        ('info', '信息'),
        ('warning', '警告'),
        ('error', '错误'),
        ('critical', '严重'),
    ])
    notification_channels = models.JSONField()  # 通知渠道列表
    is_active = models.BooleanField(default=True)

class Alert(models.Model):
    """告警记录"""
    rule = models.ForeignKey(AlertRule, on_delete=models.CASCADE)
    triggered_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True)
    status = models.CharField(choices=[
        ('active', '激活'),
        ('acknowledged', '已确认'),
        ('resolved', '已解决'),
    ])
    message = models.TextField()
    context = models.JSONField()  # 告警上下文数据

class NotificationLog(models.Model):
    """通知日志"""
    alert = models.ForeignKey(Alert, on_delete=models.CASCADE)
    channel = models.CharField(max_length=50)
    sent_at = models.DateTimeField()
    status = models.CharField(choices=[
        ('pending', '待发送'),
        ('sent', '已发送'),
        ('failed', '发送失败'),
    ])
    error_message = models.TextField(blank=True)
```

## API接口设计

### 告警规则管理
```
GET    /api/monitoring/alert-rules/          - 列出告警规则
POST   /api/monitoring/alert-rules/          - 创建告警规则
GET    /api/monitoring/alert-rules/{id}/     - 查看规则详情
PUT    /api/monitoring/alert-rules/{id}/     - 更新规则
DELETE /api/monitoring/alert-rules/{id}/     - 删除规则
POST   /api/monitoring/alert-rules/{id}/test/ - 测试规则
```

### 告警管理
```
GET    /api/monitoring/alerts/               - 列出告警
GET    /api/monitoring/alerts/active/        - 查看激活的告警
POST   /api/monitoring/alerts/{id}/acknowledge/ - 确认告警
POST   /api/monitoring/alerts/{id}/resolve/  - 解决告警
GET    /api/monitoring/alerts/stats/         - 告警统计
```

### 健康检查
```
GET    /api/monitoring/health/               - 系统健康状态
GET    /api/monitoring/health/devices/       - 设备健康状态
GET    /api/monitoring/health/services/      - 服务健康状态
```

### 性能指标
```
GET    /api/monitoring/metrics/acquisition/  - 采集性能指标
GET    /api/monitoring/metrics/system/       - 系统资源指标
GET    /api/monitoring/metrics/database/     - 数据库性能指标
```

## Celery定时任务

```python
# monitoring/tasks.py

@periodic_task(run_every=timedelta(minutes=1))
def check_device_health():
    """每分钟检查设备健康状态"""
    pass

@periodic_task(run_every=timedelta(minutes=5))
def check_data_quality():
    """每5分钟检查数据质量"""
    pass

@periodic_task(run_every=timedelta(hours=1))
def generate_performance_report():
    """每小时生成性能报告"""
    pass

@periodic_task(run_every=timedelta(days=1))
def send_daily_summary():
    """每天发送汇总报告"""
    pass
```

## 实现优先级

### P0 (必须实现)
- [ ] 基本告警规则定义
- [ ] 设备离线检测
- [ ] 邮件通知

### P1 (重要)
- [ ] 数据异常检测
- [ ] 采集失败告警
- [ ] WebHook通知
- [ ] 健康检查接口

### P2 (可选)
- [ ] 性能监控
- [ ] 数据质量监控
- [ ] 企业IM集成
- [ ] 可视化报表

## 配置示例

```python
# settings.py

# 监控配置
MONITORING_ENABLED = True

# 告警配置
ALERT_CHECK_INTERVAL = 60  # 检查间隔(秒)
ALERT_COOLDOWN = 300  # 告警冷却时间(秒)

# 邮件配置
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = 'smtp.gmail.com'
EMAIL_PORT = 587
EMAIL_USE_TLS = True
EMAIL_HOST_USER = 'your-email@gmail.com'
EMAIL_HOST_PASSWORD = 'your-password'
DEFAULT_FROM_EMAIL = 'your-email@gmail.com'

# 短信配置
SMS_PROVIDER = 'aliyun'  # 阿里云短信
SMS_ACCESS_KEY = 'xxx'
SMS_ACCESS_SECRET = 'xxx'
SMS_SIGN_NAME = '采集平台'

# WebHook配置
WEBHOOK_TIMEOUT = 5  # 超时时间(秒)
WEBHOOK_RETRY = 3  # 重试次数
```

## 使用示例

```python
# 创建告警规则
from monitoring.models import AlertRule

rule = AlertRule.objects.create(
    name='设备A离线告警',
    rule_type='device_offline',
    condition={
        'device_id': 1,
        'offline_duration': 300,  # 5分钟
    },
    threshold=1,
    severity='error',
    notification_channels=['email', 'webhook'],
    is_active=True
)

# 手动触发告警
from monitoring.services import AlertService

service = AlertService()
service.trigger_alert(
    rule=rule,
    message='设备A已离线超过5分钟',
    context={'device_id': 1, 'last_seen': '2025-10-09T10:00:00Z'}
)
```

## 前端界面设计

### 告警中心页面
- 告警列表（激活/已确认/已解决）
- 告警详情查看
- 告警确认/解决操作
- 告警统计图表

### 监控大屏页面
- 实时设备状态
- 系统资源监控
- 采集性能指标
- 告警实时滚动

### 规则配置页面
- 告警规则CRUD
- 规则测试功能
- 通知渠道配置

## 技术栈

- **Django Signals**: 自动触发监控检查
- **Celery Beat**: 定时任务调度
- **Redis**: 缓存监控数据
- **Prometheus** (可选): 指标收集
- **Grafana** (可选): 可视化监控

## 下一步

1. 根据业务需求选择P0功能实施
2. 设计详细的告警规则逻辑
3. 实现通知渠道（优先邮件）
4. 开发前端监控界面
5. 编写测试用例
6. 部署到生产环境并调优
