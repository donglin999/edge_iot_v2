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
