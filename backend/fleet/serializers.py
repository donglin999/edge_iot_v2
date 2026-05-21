from rest_framework import serializers

from .models import EdgeNode


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
