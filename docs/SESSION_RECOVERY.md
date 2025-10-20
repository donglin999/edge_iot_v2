# 采集任务自动恢复机制

## 概述

系统实现了**采集任务自动恢复机制**,确保Django重启后能够自动恢复之前运行的采集任务,保持系统的整体一致性。

## 核心功能

### 1. Django启动时自动恢复

**实现位置**: `backend/acquisition/apps.py:28-90`

当Django启动时,系统会:
1. 查找所有状态为`running`的采集会话
2. 撤销旧的Celery任务(如果存在)
3. 为每个会话创建新的Celery任务
4. 更新会话的`celery_task_id`字段
5. 如果恢复失败,将会话标记为`error`状态

**代码逻辑**:
```python
def _recover_sessions(self):
    # 查找所有运行中的会话
    running_sessions = AcquisitionSession.objects.filter(
        status=AcquisitionSession.STATUS_RUNNING
    )

    for session in running_sessions:
        # 撤销旧任务
        if session.celery_task_id:
            AsyncResult(session.celery_task_id).revoke(terminate=True)

        # 启动新任务
        celery_task = tasks.run_acquisition_task.delay(session.id)
        session.celery_task_id = celery_task.id
        session.save()
```

**日志示例**:
```
INFO apps Found 2 running sessions to recover
INFO apps Recovering session 36 for task task-海天注塑机
INFO apps Successfully recovered session 36 with new Celery task abc123...
INFO apps Session recovery completed
```

### 2. Django停止时优雅关闭

**实现位置**: `backend/acquisition/apps.py:95-154`

当Django接收到关闭信号(SIGTERM或SIGINT)时,系统会:
1. 拦截关闭信号(Ctrl+C 或 kill命令)
2. 查找所有运行中的采集会话
3. 撤销对应的Celery任务
4. **保持会话状态为`running`**(以便下次启动时恢复)
5. 继续正常关闭流程

**代码逻辑**:
```python
def shutdown_handler(signum, frame):
    # 停止所有Celery任务
    for session in running_sessions:
        if session.celery_task_id:
            AsyncResult(session.celery_task_id).revoke(terminate=True)

        # 保持status=running,以便恢复
        # 不修改数据库状态
```

**日志示例**:
```
INFO apps Received SIGTERM, stopping all acquisition sessions...
INFO apps Stopping 2 running sessions
INFO apps Revoked Celery task abc123... for session 36
INFO apps Session 36 will be recovered on next startup
INFO apps All acquisition sessions stopped
```

### 3. 信号处理器注册

系统在Django启动时注册了SIGTERM和SIGINT信号处理器:

```python
def ready(self):
    # 注册关闭处理器
    self._register_shutdown_handler()

    # 恢复运行中的会话
    self._recover_sessions()
```

**支持的信号**:
- `SIGTERM`: 正常终止信号(kill命令默认信号)
- `SIGINT`: 中断信号(Ctrl+C)

## 工作流程

### 正常运行场景

```
1. 用户启动采集任务
   └─> Session状态: running
   └─> Celery任务: 正在执行

2. Django正常运行
   └─> 采集任务持续运行
   └─> 数据持续写入InfluxDB

3. 用户手动停止任务
   └─> Session状态: stopped
   └─> Celery任务: 撤销
```

### Django重启场景

```
1. 用户发送关闭信号(Ctrl+C)
   └─> 触发shutdown_handler
   └─> 撤销所有Celery任务
   └─> Session状态: 保持running
   └─> Django进程退出

2. 用户重启Django
   └─> 触发_recover_sessions
   └─> 查找status=running的会话
   └─> 为每个会话创建新的Celery任务
   └─> 采集任务自动恢复运行

3. 系统恢复正常
   └─> 采集任务继续运行
   └─> 数据继续写入InfluxDB
```

### 异常恢复场景

```
1. Django异常崩溃(未正常关闭)
   └─> Session状态: 保持running
   └─> Celery任务: 可能仍在运行或已停止

2. 用户重启Django
   └─> 触发_recover_sessions
   └─> 撤销旧的Celery任务(如果存在)
   └─> 创建新的Celery任务
   └─> 采集任务恢复运行

3. 如果恢复失败
   └─> Session状态: error
   └─> error_message: "Failed to recover after restart: ..."
   └─> 用户需要手动检查和重启
```

## 使用方法

### 重启服务

使用提供的脚本重启服务:

```bash
./restart_services.sh
```

或手动重启:

```bash
# 停止服务
pkill -f "python3.*manage.py runserver"
pkill -f "celery.*worker"

# 启动Django
PYTHONPATH=/mnt/c/Users/dongl/Downloads/0907/0907/edge_iot_v2/backend \
DJANGO_SETTINGS_MODULE=control_plane.settings \
python3 backend/manage.py runserver > /tmp/django.log 2>&1 &

# 启动Celery
cd backend && \
DJANGO_SETTINGS_MODULE=control_plane.settings \
celery -A control_plane worker -l info --pool=solo > /tmp/celery.log 2>&1 &
```

### 查看恢复日志

```bash
# 查看Django启动日志
tail -f /tmp/django.log | grep -i "recover\|session"

# 查看Celery日志
tail -f /tmp/celery.log | grep -i "acquisition\|task"
```

### 检查会话状态

```bash
# 查看所有运行中的会话
curl -s "http://localhost:8000/api/acquisition/sessions/active/" | python3 -m json.tool

# 查看特定会话状态
curl -s "http://localhost:8000/api/acquisition/sessions/{session_id}/status/" | python3 -m json.tool
```

## 配置选项

### 禁用自动恢复

如果需要禁用自动恢复功能,可以设置环境变量:

```bash
export DISABLE_SESSION_RECOVERY=true
```

然后修改 `apps.py`:

```python
def ready(self):
    import os
    if os.environ.get('DISABLE_SESSION_RECOVERY') == 'true':
        return

    # 正常恢复流程...
```

### 恢复超时配置

目前使用Celery的默认超时设置。如需调整,可以在`control_plane/celery.py`中配置:

```python
# Celery任务超时配置
task_soft_time_limit = 3600  # 1小时软超时
task_time_limit = 3660       # 1小时+1分钟硬超时
```

## 注意事项

### 1. Celery必须在运行

恢复机制依赖Celery服务,确保:
- Celery worker正在运行
- Redis连接正常
- Celery能够接收新任务

### 2. 数据库一致性

- 会话状态存储在SQLite3数据库中
- 如果数据库文件损坏,恢复功能将失败
- 建议定期备份`backend/db.sqlite3`

### 3. InfluxDB连接

- 恢复的任务会重新连接InfluxDB
- 如果InfluxDB不可用,任务会标记为错误
- 确保InfluxDB容器在Django之前启动

### 4. 网络问题

- 如果设备网络不可达,任务会记录错误但继续运行
- 系统会自动重试连接(最多3次)
- 查看`metadata.device_health`字段了解设备状态

### 5. 开发环境注意

Django开发服务器的自动重载功能会触发多次恢复:
- 第一次:主进程启动
- 第二次:重载进程启动(RUN_MAIN=true)

代码中已处理此问题:
```python
if os.environ.get('RUN_MAIN') != 'true':
    return  # 跳过主进程的恢复
```

## 故障排查

### 问题1: 会话未自动恢复

**检查步骤**:
1. 查看Django启动日志: `grep "recover" /tmp/django.log`
2. 确认会话状态: `SELECT id, status FROM acquisition_acquisitionsession WHERE status='running';`
3. 检查Celery是否运行: `pgrep -af "celery.*worker"`

**常见原因**:
- Celery未启动
- Redis连接失败
- 数据库锁定

### 问题2: 恢复后任务立即失败

**检查步骤**:
1. 查看会话错误信息: `SELECT error_message FROM acquisition_acquisitionsession WHERE id=?;`
2. 查看Celery日志: `tail -100 /tmp/celery.log`
3. 检查设备连接: `curl "http://localhost:8000/api/config/devices/{device_id}/"`

**常见原因**:
- 设备不可达
- InfluxDB认证失败
- 配置错误

### 问题3: 重复任务

**检查步骤**:
1. 查看活跃会话: `curl "http://localhost:8000/api/acquisition/sessions/active/"`
2. 检查Celery任务: `celery -A control_plane inspect active`

**解决方法**:
```bash
# 停止所有重复任务
for session_id in $(curl -s "http://localhost:8000/api/acquisition/sessions/active/" | jq '.[].id'); do
    curl -X POST "http://localhost:8000/api/acquisition/sessions/${session_id}/stop/"
done

# 重启服务
./restart_services.sh
```

## 最佳实践

### 1. 优雅关闭

始终使用信号而不是强制终止:
```bash
# 推荐
pkill -SIGTERM -f "manage.py runserver"

# 避免
pkill -9 -f "manage.py runserver"  # 会绕过信号处理器
```

### 2. 监控恢复状态

在生产环境中,添加监控脚本:
```bash
#!/bin/bash
# check_recovery.sh

EXPECTED=$(sqlite3 db.sqlite3 "SELECT COUNT(*) FROM acquisition_acquisitionsession WHERE status='running';")
ACTUAL=$(curl -s http://localhost:8000/api/acquisition/sessions/active/ | jq 'length')

if [ "$EXPECTED" -ne "$ACTUAL" ]; then
    echo "WARNING: Expected $EXPECTED running sessions but found $ACTUAL"
    exit 1
fi
```

### 3. 定期清理

定期清理已停止的旧会话:
```python
# management command: cleanup_sessions.py
from django.core.management.base import BaseCommand
from acquisition.models import AcquisitionSession
from datetime import timedelta
from django.utils import timezone

class Command(BaseCommand):
    def handle(self, *args, **options):
        # 删除30天前停止的会话
        cutoff = timezone.now() - timedelta(days=30)
        old_sessions = AcquisitionSession.objects.filter(
            status__in=['stopped', 'error'],
            updated_at__lt=cutoff
        )
        count = old_sessions.count()
        old_sessions.delete()
        self.stdout.write(f"Deleted {count} old sessions")
```

## 参考

- [Django Signals文档](https://docs.djangoproject.com/en/4.2/topics/signals/)
- [Celery任务管理](https://docs.celeryq.dev/en/stable/userguide/tasks.html)
- [Python信号处理](https://docs.python.org/3/library/signal.html)
