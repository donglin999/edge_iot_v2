"""Core configuration models for the control plane."""
from __future__ import annotations

from decimal import Decimal

from django.db import models


class TimeStampedModel(models.Model):
    """Abstract base model that tracks creation and update times."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Site(TimeStampedModel):
    """Represents a physical or logical site."""

    code = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=128)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["code"]

    def __str__(self) -> str:
        return f"{self.code} - {self.name}"


class Device(TimeStampedModel):
    """Represents a data acquisition connection endpoint."""

    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="devices")
    name = models.CharField(max_length=128)
    code = models.CharField(max_length=128)
    protocol = models.CharField(max_length=32)
    ip_address = models.GenericIPAddressField(blank=True, null=True)
    port = models.PositiveIntegerField(blank=True, null=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        unique_together = ("site", "protocol", "ip_address", "port")
        ordering = ["site", "code"]

    def __str__(self) -> str:
        return f"{self.site.code}:{self.code}"


class Channel(TimeStampedModel):
    """Logical/physical channel definition for a device."""

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="channels")
    name = models.CharField(max_length=64)
    number = models.PositiveIntegerField()
    sampling_rate_hz = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("1.00"))
    config = models.JSONField(default=dict, blank=True)

    class Meta:
        unique_together = ("device", "number")
        ordering = ["device", "number"]

    def __str__(self) -> str:
        return f"{self.device}:{self.number}"


class PointTemplate(TimeStampedModel):
    """Reusable template describing common point attributes."""

    name = models.CharField(max_length=128)
    english_name = models.CharField(max_length=128)
    unit = models.CharField(max_length=32, blank=True)
    data_type = models.CharField(max_length=32, default="float")
    coefficient = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("1.0000"))
    precision = models.PositiveSmallIntegerField(default=2)

    class Meta:
        unique_together = ("name", "english_name")
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Point(TimeStampedModel):
    """Concrete acquisition point bound to a device and optional channel."""

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="points")
    channel = models.ForeignKey(Channel, on_delete=models.SET_NULL, related_name="points", blank=True, null=True)
    template = models.ForeignKey(PointTemplate, on_delete=models.SET_NULL, related_name="points", blank=True, null=True)
    code = models.CharField(max_length=128)
    address = models.CharField(max_length=128)
    description = models.CharField(max_length=255, blank=True)
    sample_rate_hz = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("1.00"))
    extra = models.JSONField(default=dict, blank=True)

    class Meta:
        unique_together = ("device", "code")
        ordering = ["device", "code"]

    def __str__(self) -> str:
        return f"{self.device}:{self.code}"


class AcqTask(TimeStampedModel):
    """Logical acquisition task definition."""

    code = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=128)
    description = models.TextField(blank=True)
    schedule = models.CharField(max_length=64, default="continuous")
    is_active = models.BooleanField(default=True)

    points = models.ManyToManyField(Point, through="TaskPoint", related_name="tasks")

    class Meta:
        ordering = ["code"]

    def __str__(self) -> str:
        return self.name


class TaskPoint(models.Model):
    """Join table for tasks and points with optional per-task overrides."""

    task = models.ForeignKey(AcqTask, on_delete=models.CASCADE)
    point = models.ForeignKey(Point, on_delete=models.CASCADE)
    overrides = models.JSONField(default=dict, blank=True)

    class Meta:
        unique_together = ("task", "point")

    def __str__(self) -> str:
        return f"{self.task.code}:{self.point.code}"


class ConfigVersion(TimeStampedModel):
    """Captured configuration snapshot for traceability and rollback."""

    task = models.ForeignKey(AcqTask, on_delete=models.CASCADE, related_name="versions")
    version = models.PositiveIntegerField()
    summary = models.TextField(blank=True)
    created_by = models.CharField(max_length=128, blank=True)
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        unique_together = ("task", "version")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.task.code}@{self.version}"


class ImportJob(TimeStampedModel):
    """Tracks Excel import attempts and results."""

    STATUS_PENDING = "pending"
    STATUS_VALIDATED = "validated"
    STATUS_FAILED = "failed"
    STATUS_APPLIED = "applied"

    STATUS_CHOICES = [
        (STATUS_PENDING, "待处理"),
        (STATUS_VALIDATED, "已校验"),
        (STATUS_FAILED, "失败"),
        (STATUS_APPLIED, "已生效"),
    ]

    source_name = models.CharField(max_length=255, blank=True)
    triggered_by = models.CharField(max_length=128, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING)
    summary = models.JSONField(default=dict, blank=True)
    related_version = models.ForeignKey(ConfigVersion, on_delete=models.SET_NULL, blank=True, null=True, related_name="import_jobs")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.source_name or self.id} ({self.status})"


class WorkerEndpoint(TimeStampedModel):
    """Represents a worker process host for acquisition tasks."""

    identifier = models.CharField(max_length=128, unique=True)
    host = models.CharField(max_length=128)
    description = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=32, default="unknown")
    last_seen_at = models.DateTimeField(blank=True, null=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["identifier"]

    def __str__(self) -> str:
        return self.identifier


class TaskRun(TimeStampedModel):
    """Records runtime status of acquisition tasks."""

    STATUS_PENDING = "pending"
    STATUS_RUNNING = "running"
    STATUS_SUCCEEDED = "succeeded"
    STATUS_FAILED = "failed"
    STATUS_STOPPED = "stopped"

    STATUS_CHOICES = [
        (STATUS_PENDING, "待启动"),
        (STATUS_RUNNING, "运行中"),
        (STATUS_SUCCEEDED, "成功"),
        (STATUS_FAILED, "失败"),
        (STATUS_STOPPED, "已停止"),
    ]

    task = models.ForeignKey(AcqTask, on_delete=models.CASCADE, related_name="runs")
    config_version = models.ForeignKey(ConfigVersion, on_delete=models.SET_NULL, blank=True, null=True, related_name="runs")
    worker = models.ForeignKey(WorkerEndpoint, on_delete=models.SET_NULL, blank=True, null=True, related_name="runs")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending")
    started_at = models.DateTimeField(blank=True, null=True)
    finished_at = models.DateTimeField(blank=True, null=True)
    log_reference = models.CharField(max_length=255, blank=True)
    context = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.task.code}:{self.status}"
