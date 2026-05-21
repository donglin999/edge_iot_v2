"""DRF views for the fleet registry.

Exposes:
- GET  /api/fleet/edges/                                — list edges (auto-sweeps stale → offline)
- POST /api/fleet/edges/                                — factory-register a new edge, returns
                                                          the one-shot activation token
- GET  /api/fleet/edges/<id>/                           — single edge detail
- POST /api/fleet/edges/<id>/assignments/sync           — reconcile EdgeAssignment rows for
                                                          the edge and push an ``apply_config``
                                                          frame (M2)
"""
from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import EdgeNode, EdgeTaskStatus
from .serializers import (
    EdgeAssignmentSerializer,
    EdgeNodeCreateSerializer,
    EdgeNodeSerializer,
    EdgeTaskStatusSerializer,
)
from .services import sweep_stale_edges, sync_assignments


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

    @extend_schema(
        summary="同步任务分派到指定 edge",
        description=(
            "Reconcile EdgeAssignment rows against AcqTask.edge_id and push a "
            "full apply_config snapshot to the edge over its open WS. Returns "
            "the new per-edge config_version and the assembled task/device/"
            "point counts. If the edge is offline the snapshot is still "
            "computed but not delivered — the edge will pick it up on next "
            "register."
        ),
        responses={200: OpenApiResponse(description="同步结果摘要")},
    )
    @action(detail=True, methods=["post"], url_path="assignments/sync")
    def assignments_sync(self, request, pk=None):
        edge = self.get_object()
        result = sync_assignments(edge)
        # Frame can be large; keep it under a top-level key so callers can
        # opt in to inspecting it without paying for it in routine summaries.
        return Response(result, status=status.HTTP_200_OK)

    @extend_schema(
        summary="列出 edge 已分派的任务",
        responses={200: EdgeAssignmentSerializer(many=True)},
    )
    @action(detail=True, methods=["get"], url_path="assignments")
    def assignments(self, request, pk=None):
        edge = self.get_object()
        qs = edge.assignments.select_related("task").order_by("task__code")
        ser = EdgeAssignmentSerializer(qs, many=True)
        return Response(ser.data)


@extend_schema(summary="列出 edge 上报的任务运行状态")
class EdgeTaskStatusViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """Read-only projection of the latest per-(edge, task) lifecycle state.

    The center frontend's ``/acquisition`` page joins this against the
    task list to render "task X is running on edge Y". The rows are kept
    fresh by the inbound ``task_state`` WS frames. Filter with
    ``?task=<id>`` / ``?edge=<id>`` for the per-task lookup.
    """

    serializer_class = EdgeTaskStatusSerializer
    queryset = EdgeTaskStatus.objects.select_related("edge", "task").order_by(
        "edge", "task"
    )

    def get_queryset(self):
        qs = super().get_queryset()
        task_id = self.request.query_params.get("task")
        edge_id = self.request.query_params.get("edge")
        if task_id:
            qs = qs.filter(task_id=task_id)
        if edge_id:
            qs = qs.filter(edge_id=edge_id)
        return qs
