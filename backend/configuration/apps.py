from django.apps import AppConfig
from django.db.backends.signals import connection_created
from django.dispatch import receiver


@receiver(connection_created)
def _tune_sqlite(sender, connection, **kwargs):
    """SQLite 并发加固:WAL + busy_timeout。

    现场部署是 django + celery-acq + celery-short(含 beat)三个进程共用同一个
    db 文件(docker 卷),默认回滚日志模式下读写互斥,前端 3s 轮询叠加采集
    心跳写(每 10s)就能锁出「database is locked」——后果链在现场实测过:
    心跳写失败 → 看门狗误判会话死亡;_should_continue 的 refresh 抛错 →
    会话自行退出 → 约 120s 一次的重启循环。

    WAL 让读不阻塞写、写不阻塞读(同一文件系统上的多进程共享,docker 卷满足);
    busy_timeout 与 settings 的 OPTIONS.timeout 同值兜底。每个新连接都会收到
    此信号,PRAGMA 幂等,重复执行无害。
    """
    if connection.vendor != "sqlite":
        return
    cursor = connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA busy_timeout=30000;")
    # NORMAL 在 WAL 下仅在 checkpoint 时 fsync,掉电最多丢最近批次,配置库可接受;
    # 换来的是写延迟大幅下降,进一步压缩锁窗口。
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.close()


class ConfigurationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "configuration"
    verbose_name = "????"

    def ready(self):
        from . import signals  # noqa: F401
