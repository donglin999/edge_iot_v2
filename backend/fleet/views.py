"""DRF views for the fleet registry.

Exposes:
- GET  /api/fleet/edges/                — list edges (auto-sweeps stale → offline)
- POST /api/fleet/edges/                — factory-register a new edge, returns
                                          the one-shot activation token
- GET  /api/fleet/edges/<id>/           — single edge detail
"""
from __future__ import annotations

from drf_spectacular.utils import extend_schema, OpenApiResponse
from rest_framework import mixins, status, viewsets
from rest_framework.response import Response

from .models import EdgeNode
from .serializers import EdgeNodeCreateSerializer, EdgeNodeSerializer
from .services import sweep_stale_edges


class EdgeNodeViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    queryset = EdgeNode.objects.all()
    serializer_class = EdgeNodeSerializer

    @extend_schema(summary="列出已注册 edge 节点", description="返回前会扫一次 last_seen,超时节点标 offline")
    def list(self, request, *args, **kwargs):
        sweep_stale_edges()
        return super().list(request, *args, **kwargs)

    @extend_schema(
        summary="出厂注册一个新的 edge 节点",
        request=EdgeNodeCreateSerializer,
        responses={201: OpenApiResponse(description="返回 edge 元数据 + 一次性 activation_token")},
    )
    def create(self, request, *args, **kwargs):
        ser = EdgeNodeCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        node, token = EdgeNode.issue(
            name=ser.validated_data["name"],
            labels=ser.validated_data.get("labels"),
        )
        body = EdgeNodeSerializer(node).data
        body["activation_token"] = token
        return Response(body, status=status.HTTP_201_CREATED)
