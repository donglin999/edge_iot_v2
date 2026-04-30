"""Serializers for configuration APIs."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from rest_framework import serializers

from . import models


class SiteSerializer(serializers.ModelSerializer):
    """站点序列化：包含站点编码、名称与描述。"""

    class Meta:
        model = models.Site
        fields = ["id", "code", "name", "description", "created_at", "updated_at"]
        read_only_fields = ("id", "created_at", "updated_at")


class DeviceSerializer(serializers.ModelSerializer):
    """采集连接端点序列化：包含协议、IP、端口等信息。"""

    site = serializers.PrimaryKeyRelatedField(queryset=models.Site.objects.all())

    class Meta:
        model = models.Device
        fields = [
            "id",
            "site",
            "code",
            "name",
            "protocol",
            "ip_address",
            "port",
            "metadata",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ("id", "created_at", "updated_at")


class ChannelSerializer(serializers.ModelSerializer):
    """通道序列化：描述连接下的测量通道。"""

    device = serializers.PrimaryKeyRelatedField(queryset=models.Device.objects.all())

    class Meta:
        model = models.Channel
        fields = ["id", "device", "name", "number", "sampling_rate_hz", "config", "created_at", "updated_at"]
        read_only_fields = ("id", "created_at", "updated_at")


class PointTemplateSerializer(serializers.ModelSerializer):
    """测点模板序列化：定义可复用的测点属性。"""

    class Meta:
        model = models.PointTemplate
        fields = ["id", "name", "english_name", "unit", "data_type", "coefficient", "precision", "created_at", "updated_at"]
        read_only_fields = ("id", "created_at", "updated_at")


class PointSerializer(serializers.ModelSerializer):
    """测点序列化：绑定设备、模板等信息。"""

    device = serializers.PrimaryKeyRelatedField(queryset=models.Device.objects.all())
    channel = serializers.PrimaryKeyRelatedField(queryset=models.Channel.objects.all(), allow_null=True, required=False)
    template_detail = PointTemplateSerializer(source="template", read_only=True)

    class Meta:
        model = models.Point
        fields = [
            "id",
            "device",
            "channel",
            "template",
            "template_detail",
            "code",
            "address",
            "description",
            "sample_rate_hz",
            "extra",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ("id", "created_at", "updated_at", "template_detail")


class TaskPointSerializer(serializers.ModelSerializer):
    """任务与测点关联序列化。"""

    class Meta:
        model = models.TaskPoint
        fields = ["id", "task", "point", "overrides"]
        read_only_fields = ("id",)


# Physical / practical upper bound for sample_rate_hz per protocol.
# A task whose points span multiple protocols is constrained by the
# *minimum* of the involved protocols' caps.
PROTOCOL_MAX_HZ = {
    "modbus_tcp": 50.0,    # 50ms 周期，TCP 极限
    "modbus_rtu": 5.0,     # 串口 9600bps 物理极限
    "mqtt": 100.0,         # 推送，没有 polling 概念，宽松
    "opcua": 50.0,         # subscription 也按 50ms 起
    "siemens_s7": 20.0,    # snap7 sync ~20Hz 稳
}


class AcqTaskSerializer(serializers.ModelSerializer):
    """采集任务序列化：定义任务编码、调度及测点集合。"""

    points = serializers.PrimaryKeyRelatedField(queryset=models.Point.objects.all(), many=True, required=False)
    sample_rate_hz = serializers.DecimalField(
        max_digits=8,
        decimal_places=2,
        min_value=Decimal("0.1"),
        max_value=Decimal("100"),
        required=False,
        help_text="采集频率(Hz)，0.1~100，修改后将自动重启运行中的会话。",
    )

    class Meta:
        model = models.AcqTask
        fields = [
            "id",
            "code",
            "name",
            "description",
            "sample_rate_hz",
            "is_active",
            "points",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ("id", "created_at", "updated_at")

    def validate(self, attrs):
        attrs = super().validate(attrs)

        # Determine the requested rate. On PATCH/PUT we compare against the
        # incoming value if present, else fall back to the existing instance.
        rate = attrs.get("sample_rate_hz")
        if rate is None and self.instance is not None:
            rate = getattr(self.instance, "sample_rate_hz", None)
        if rate is None:
            return attrs

        # Determine the protocols actually in play. Prefer the points being
        # assigned in this request; otherwise fall back to the instance's
        # current M2M membership. New tasks with no points yet skip this
        # validation entirely (we have nothing concrete to constrain).
        incoming_points = attrs.get("points")
        if incoming_points:
            protocols = {p.device.protocol for p in incoming_points if p.device_id}
        elif self.instance is not None and self.instance.pk:
            protocols = set(
                self.instance.points
                .values_list("device__protocol", flat=True)
                .distinct()
            )
        else:
            protocols = set()

        if not protocols:
            return attrs

        rate_f = float(rate)
        # Find the tightest protocol constraint and the protocol that
        # imposed it, so the error message can name the offender.
        worst_protocol = None
        worst_limit = None
        for proto in protocols:
            cap = PROTOCOL_MAX_HZ.get(proto, 100.0)
            if worst_limit is None or cap < worst_limit:
                worst_limit = cap
                worst_protocol = proto

        if worst_limit is not None and rate_f > worst_limit:
            raise serializers.ValidationError({
                "sample_rate_hz": (
                    f"协议 {worst_protocol} 不支持高于 {worst_limit} Hz 的采集频率"
                ),
            })
        return attrs

    def create(self, validated_data):
        points = validated_data.pop("points", [])
        task = super().create(validated_data)
        if points:
            task.points.set(points)
        return task

    def update(self, instance, validated_data):
        points = validated_data.pop("points", None)
        task = super().update(instance, validated_data)
        if points is not None:
            task.points.set(points)
        return task


class TaskOverviewSerializer(serializers.Serializer):
    """采集任务概览序列化，用于运行监控。"""

    total_tasks = serializers.IntegerField()
    active_tasks = serializers.IntegerField()
    status = serializers.DictField(child=serializers.IntegerField())
    recent_runs = serializers.ListField(child=serializers.DictField(), help_text="最近 24 小时内的任务运行记录")
    generated_at = serializers.DateTimeField(help_text="生成时间")


class ImportJobSerializer(serializers.ModelSerializer):
    """导入作业只读序列化：返回状态与校验结果。"""

    class Meta:
        model = models.ImportJob
        fields = ["id", "source_name", "triggered_by", "status", "summary", "related_version", "created_at", "updated_at"]
        read_only_fields = ("id", "source_name", "status", "summary", "related_version", "created_at", "updated_at")


class ImportJobCreateSerializer(ImportJobSerializer):
    """导入作业创建：上传 Excel 文件即可触发校验。"""

    file = serializers.FileField(write_only=True, help_text="上传 Excel 文件，可拖拽或点击选择")

    class Meta(ImportJobSerializer.Meta):
        fields = tuple(ImportJobSerializer.Meta.fields) + ("file",)
        read_only_fields = ImportJobSerializer.Meta.read_only_fields

    def validate_file(self, upload):
        name = upload.name.lower()
        if not name.endswith((".xlsx", ".xls")):
            raise serializers.ValidationError("仅支持 Excel 文件上传 (.xlsx/.xls)")
        if upload.size == 0:
            raise serializers.ValidationError("文件内容为空")
        self._original_name = upload.name
        return upload

    def create(self, validated_data):
        upload = validated_data.pop("file")
        original_name = getattr(self, "_original_name", upload.name)
        if not validated_data.get("source_name"):
            validated_data["source_name"] = original_name

        job = models.ImportJob.objects.create(**validated_data)
        storage_dir = Path(settings.BASE_DIR) / "uploads" / "import_jobs"
        storage_dir.mkdir(parents=True, exist_ok=True)
        saved_path = storage_dir / f"{job.id}_{original_name}"
        with saved_path.open("wb") as dest:
            for chunk in upload.chunks():
                dest.write(chunk)

        job.summary = {
            "file_path": str(saved_path),
            "original_name": original_name,
        }
        job.save(update_fields=["summary"])
        self._saved_path = saved_path
        return job

    @property
    def saved_path(self) -> Path:
        return getattr(self, "_saved_path")


class ConfigVersionSerializer(serializers.ModelSerializer):
    """配置版本序列化：用于追踪任务快照。"""

    class Meta:
        model = models.ConfigVersion
        fields = ["id", "task", "version", "summary", "created_by", "payload", "created_at", "updated_at"]
        read_only_fields = ("id", "created_at", "updated_at")


class ImportDiffSerializer(serializers.Serializer):
    site_code = serializers.CharField()
    connections = serializers.JSONField()
    points = serializers.JSONField()


class TaskControlSerializer(serializers.Serializer):
    worker = serializers.CharField(required=False, allow_blank=True)
    note = serializers.CharField(required=False, allow_blank=True)
