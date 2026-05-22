from rest_framework import serializers

from .models import (
    EdgeAssignment,
    EdgeLifecycleEvent,
    EdgeNode,
    EdgeSample,
    EdgeTaskStatus,
)


class EdgeNodeSerializer(serializers.ModelSerializer):
    class Meta:
        model = EdgeNode
        fields = (
            "id",
            "name",
            "status",
            "version",
            "labels",
            "last_seen",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class EdgeNodeCreateSerializer(serializers.Serializer):
    """Factory-register payload — operator declares the new edge by name."""

    name = serializers.CharField(max_length=128)
    labels = serializers.DictField(child=serializers.CharField(), required=False)


class EdgeAssignmentSerializer(serializers.ModelSerializer):
    """Read-only view of an edge's currently dispatched tasks."""

    task_code = serializers.CharField(source="task.code", read_only=True)
    task_name = serializers.CharField(source="task.name", read_only=True)

    class Meta:
        model = EdgeAssignment
        fields = (
            "id",
            "edge",
            "task",
            "task_code",
            "task_name",
            "desired_state",
            "config_version",
            "last_applied_version",
            "applied_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields


class EdgeTaskStatusSerializer(serializers.ModelSerializer):
    """Latest reported lifecycle state for an (edge, task) pairing."""

    edge_name = serializers.CharField(source="edge.name", read_only=True)
    task_code = serializers.CharField(source="task.code", read_only=True)

    class Meta:
        model = EdgeTaskStatus
        fields = (
            "id",
            "edge",
            "edge_name",
            "task",
            "task_code",
            "state",
            "error",
            "last_reported_at",
            "updated_at",
        )
        read_only_fields = fields


class EdgeLifecycleEventSerializer(serializers.ModelSerializer):
    """One row of an edge's lifecycle audit timeline (M3)."""

    edge_name = serializers.CharField(source="edge.name", read_only=True)
    task_code = serializers.CharField(source="task.code", read_only=True, default=None)

    class Meta:
        model = EdgeLifecycleEvent
        fields = (
            "id",
            "edge",
            "edge_name",
            "task",
            "task_code",
            "event",
            "monotonic_seq",
            "error",
            "edge_ts",
            "received_at",
        )
        read_only_fields = fields


class EdgeSampleSerializer(serializers.ModelSerializer):
    """Latest cached sample for an (edge, task, point) — the 汇聚缓存 (M3)."""

    edge_name = serializers.CharField(source="edge.name", read_only=True)
    task_code = serializers.CharField(source="task.code", read_only=True, default=None)

    class Meta:
        model = EdgeSample
        fields = (
            "id",
            "edge",
            "edge_name",
            "task",
            "task_code",
            "point_code",
            "value",
            "quality",
            "sample_ts",
            "monotonic_seq",
            "window_end",
            "updated_at",
        )
        read_only_fields = fields
