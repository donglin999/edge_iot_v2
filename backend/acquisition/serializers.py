"""Serializers for acquisition API."""
from __future__ import annotations

from rest_framework import serializers
from django.utils import timezone

from acquisition import models as acq_models
from configuration import models as config_models


class AcquisitionSessionSerializer(serializers.ModelSerializer):
    """采集会话序列化器"""

    task_code = serializers.CharField(source='task.code', read_only=True)
    task_name = serializers.CharField(source='task.name', read_only=True)
    worker_identifier = serializers.CharField(source='worker.identifier', read_only=True, allow_null=True)
    duration_seconds = serializers.SerializerMethodField()

    class Meta:
        model = acq_models.AcquisitionSession
        fields = [
            'id',
            'task',
            'task_code',
            'task_name',
            'worker',
            'worker_identifier',
            'status',
            'celery_task_id',
            'pid',
            'started_at',
            'stopped_at',
            'duration_seconds',
            'error_message',
            'metadata',
            'created_at',
            'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_duration_seconds(self, obj):
        """计算运行时长（秒）"""
        if not obj.started_at:
            return None
        end_time = obj.stopped_at or timezone.now()
        return (end_time - obj.started_at).total_seconds()


class SessionStatusSerializer(serializers.Serializer):
    """会话状态详情序列化器"""

    session_id = serializers.IntegerField()
    task_code = serializers.CharField()
    task_name = serializers.CharField()
    status = serializers.CharField()
    celery_task_id = serializers.CharField(allow_null=True)
    started_at = serializers.DateTimeField(allow_null=True)
    stopped_at = serializers.DateTimeField(allow_null=True)
    duration_seconds = serializers.FloatField(allow_null=True)
    points_read = serializers.IntegerField()
    last_read_time = serializers.DateTimeField(allow_null=True)
    error_count = serializers.IntegerField()
    error_message = serializers.CharField(allow_blank=True)
    metadata = serializers.JSONField()


class StartTaskSerializer(serializers.Serializer):
    """启动采集任务请求序列化器"""

    task_id = serializers.IntegerField(required=True)
    config_version_id = serializers.IntegerField(required=False, allow_null=True)
    worker_identifier = serializers.CharField(required=False, allow_blank=True, max_length=255)
    metadata = serializers.JSONField(required=False, default=dict)

    def validate_task_id(self, value):
        """验证任务ID是否存在"""
        try:
            task = config_models.AcqTask.objects.get(pk=value)
            if not task.is_active:
                raise serializers.ValidationError(f"任务 {task.code} 未激活")
        except config_models.AcqTask.DoesNotExist:
            raise serializers.ValidationError(f"任务ID {value} 不存在")
        return value


class StopSessionSerializer(serializers.Serializer):
    """停止会话请求序列化器"""

    reason = serializers.CharField(required=False, allow_blank=True, max_length=500)


class DataPointSerializer(serializers.ModelSerializer):
    """数据点序列化器"""

    class Meta:
        model = acq_models.DataPoint
        fields = [
            'id',
            'session',
            'point_code',
            'timestamp',
            'value',
            'quality',
            'metadata',
            'created_at',
        ]
        read_only_fields = ['id', 'created_at']


class TestConnectionSerializer(serializers.Serializer):
    """测试设备连接请求序列化器"""

    protocol_type = serializers.CharField(required=True, max_length=50)
    device_config = serializers.JSONField(required=True)


class TestStorageSerializer(serializers.Serializer):
    """测试存储连接请求序列化器"""

    storage_type = serializers.CharField(required=True, max_length=50)
    storage_config = serializers.JSONField(required=True)


class ConnectionTestResultSerializer(serializers.Serializer):
    """连接测试结果序列化器"""

    status = serializers.CharField()
    protocol = serializers.CharField(required=False)
    storage = serializers.CharField(required=False)
    connected = serializers.BooleanField(required=False)
    healthy = serializers.BooleanField(required=False)
    error = serializers.CharField(required=False, allow_blank=True)
    details = serializers.JSONField(required=False)
