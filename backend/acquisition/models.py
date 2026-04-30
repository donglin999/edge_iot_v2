"""Models for tracking acquisition runtime state."""
from __future__ import annotations

from django.db import models
from configuration.models import TimeStampedModel, AcqTask, WorkerEndpoint


class AcquisitionSession(TimeStampedModel):
    """Tracks an active acquisition session for a task."""

    STATUS_RUNNING = "running"
    STATUS_PAUSED = "paused"
    STATUS_STOPPED = "stopped"
    STATUS_ERROR = "error"

    STATUS_CHOICES = [
        (STATUS_RUNNING, "运行中"),
        (STATUS_PAUSED, "已暂停"),
        (STATUS_STOPPED, "已停止"),
        (STATUS_ERROR, "错误"),
    ]

    task = models.ForeignKey(AcqTask, on_delete=models.CASCADE, related_name="sessions")
    worker = models.ForeignKey(WorkerEndpoint, on_delete=models.SET_NULL, null=True, blank=True, related_name="sessions")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_RUNNING)
    celery_task_id = models.CharField(max_length=255, blank=True, help_text="Celery任务ID")
    pid = models.IntegerField(null=True, blank=True, help_text="进程ID")
    started_at = models.DateTimeField(null=True, blank=True)
    stopped_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["celery_task_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.task.code} - {self.status}"


class DataPoint(TimeStampedModel):
    """Stores sampled data points from acquisition."""

    session = models.ForeignKey(AcquisitionSession, on_delete=models.CASCADE, related_name="data_points")
    point_code = models.CharField(max_length=128, help_text="测点编码")
    timestamp = models.DateTimeField(help_text="采集时间戳")
    value = models.JSONField(help_text="采集值(支持各种类型)")
    quality = models.CharField(max_length=16, default="good", help_text="数据质量: good/bad/uncertain")
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["session", "-timestamp"]),
            models.Index(fields=["point_code", "-timestamp"]),
        ]

    def __str__(self) -> str:
        return f"{self.point_code}@{self.timestamp}"


class AlarmRule(TimeStampedModel):
    """Threshold rule on a single point.

    The acquisition loop checks each reading against active rules. Triggered
    rules emit :class:`Alarm` rows + WebSocket pushes to the global stream.
    """

    OPERATORS = (
        ("gt", "> 大于"),
        ("ge", "≥ 大于等于"),
        ("lt", "< 小于"),
        ("le", "≤ 小于等于"),
        ("eq", "= 等于"),
        ("ne", "≠ 不等于"),
        ("between", "区间内"),
        ("outside", "区间外"),
    )
    SEVERITIES = (
        ("info", "提示"),
        ("warning", "警告"),
        ("critical", "严重"),
    )

    name = models.CharField(max_length=128)
    point_code = models.CharField(max_length=128, db_index=True,
                                  help_text="匹配读数中的 code 字段")
    device_code = models.CharField(max_length=255, blank=True,
                                   help_text="若指定,仅匹配此设备的同名测点")
    operator = models.CharField(max_length=16, choices=OPERATORS, default="gt")
    threshold = models.FloatField(null=True, blank=True)
    threshold_high = models.FloatField(null=True, blank=True,
                                        help_text="between/outside 操作符使用")
    severity = models.CharField(max_length=16, choices=SEVERITIES, default="warning")
    is_active = models.BooleanField(default=True)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]


class Alarm(TimeStampedModel):
    """One row per fire/clear transition of an :class:`AlarmRule`."""

    STATUS_FIRING = "firing"
    STATUS_ACKED = "acked"
    STATUS_CLEARED = "cleared"

    STATUS_CHOICES = (
        (STATUS_FIRING, "未确认"),
        (STATUS_ACKED, "已确认"),
        (STATUS_CLEARED, "已恢复"),
    )

    rule = models.ForeignKey(AlarmRule, on_delete=models.CASCADE, related_name="alarms")
    session = models.ForeignKey(AcquisitionSession, on_delete=models.CASCADE,
                                 related_name="alarms", null=True, blank=True)
    point_code = models.CharField(max_length=128)
    device_code = models.CharField(max_length=255, blank=True)
    value = models.JSONField()
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_FIRING)
    fired_at = models.DateTimeField(auto_now_add=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.CharField(max_length=64, blank=True)
    cleared_at = models.DateTimeField(null=True, blank=True)
    message = models.TextField(blank=True)

    class Meta:
        ordering = ["-fired_at"]
        indexes = [
            models.Index(fields=["status", "-fired_at"]),
            models.Index(fields=["point_code", "-fired_at"]),
        ]
