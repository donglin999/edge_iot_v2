
"""ViewSets for configuration APIs."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from django.db.models import Max
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from configuration.services.importer import ExcelImportService
from . import models, serializers, tasks


@extend_schema_view(
    list=extend_schema(summary="列出所有站点"),
    retrieve=extend_schema(summary="查看站点"),
    create=extend_schema(summary="创建站点"),
    update=extend_schema(summary="更新站点"),
    partial_update=extend_schema(summary="部分更新站点"),
    destroy=extend_schema(summary="删除站点"),
)
class SiteViewSet(viewsets.ModelViewSet):
    """站点管理：维护控制系统的厂区或逻辑区域。"""

    queryset = models.Site.objects.all().order_by("code")
    serializer_class = serializers.SiteSerializer


@extend_schema_view(
    list=extend_schema(summary="列出采集连接"),
    retrieve=extend_schema(summary="查看采集连接"),
    create=extend_schema(summary="创建采集连接"),
    update=extend_schema(summary="更新采集连接"),
    partial_update=extend_schema(summary="部分更新采集连接"),
    destroy=extend_schema(summary="删除采集连接"),
)
class DeviceViewSet(viewsets.ModelViewSet):
    """采集连接管理。"""

    queryset = models.Device.objects.select_related("site").order_by("protocol", "ip_address", "port")
    serializer_class = serializers.DeviceSerializer

    def list(self, request, *args, **kwargs):
        if request.query_params.get("distinct"):
            site_code = request.query_params.get("site_code", "default")
            seen = set()
            devices = []
            for device in models.Device.objects.filter(site__code=site_code).order_by("protocol", "ip_address", "port"):
                key = (device.protocol, device.ip_address, device.port)
                if key in seen:
                    continue
                seen.add(key)
                devices.append(device)
            serializer = self.get_serializer(devices, many=True)
            return Response(serializer.data)
        return super().list(request, *args, **kwargs)


@extend_schema_view(
    list=extend_schema(summary="列出通道"),
    retrieve=extend_schema(summary="查看通道"),
    create=extend_schema(summary="创建通道"),
    update=extend_schema(summary="更新通道"),
    partial_update=extend_schema(summary="部分更新通道"),
    destroy=extend_schema(summary="删除通道"),
)
class ChannelViewSet(viewsets.ModelViewSet):
    """通道管理：配置连接下的逻辑/物理通道。"""

    queryset = models.Channel.objects.select_related("device").order_by("device", "number")
    serializer_class = serializers.ChannelSerializer


@extend_schema_view(
    list=extend_schema(summary="列出测点"),
    retrieve=extend_schema(summary="查看测点"),
    create=extend_schema(summary="创建测点"),
    update=extend_schema(summary="更新测点"),
    partial_update=extend_schema(summary="部分更新测点"),
    destroy=extend_schema(summary="删除测点"),
)
class PointViewSet(viewsets.ModelViewSet):
    """测点管理：定义设备上的采集点及属性。"""

    queryset = models.Point.objects.select_related("device", "channel").prefetch_related("tasks").order_by("device", "code")
    serializer_class = serializers.PointSerializer


@extend_schema_view(
    list=extend_schema(summary="列出采集任务"),
    retrieve=extend_schema(summary="查看采集任务"),
    create=extend_schema(summary="创建采集任务"),
    update=extend_schema(summary="更新采集任务"),
    partial_update=extend_schema(summary="部分更新采集任务"),
    destroy=extend_schema(summary="删除采集任务"),
)
class AcqTaskViewSet(viewsets.ModelViewSet):
    """采集任务管理：维护任务及其测点。"""

    queryset = models.AcqTask.objects.prefetch_related("points").order_by("code")
    serializer_class = serializers.AcqTaskSerializer

    @extend_schema(summary="查看任务关联的测点列表", responses=serializers.PointSerializer(many=True))
    @action(detail=True, methods=["get"], url_path="points")
    def list_points(self, request, pk=None):
        task: models.AcqTask = self.get_object()
        serializer = serializers.PointSerializer(task.points.all(), many=True, context={"request": request})
        return Response(serializer.data)

    @extend_schema(summary="采集任务运行概览", responses=serializers.TaskOverviewSerializer)
    @action(detail=False, methods=["get"], url_path="overview")
    def overview(self, request):
        site_code = request.query_params.get("site_code", "default")
        now = timezone.now()
        recent_window = now - timedelta(hours=24)

        base_qs = (
            models.AcqTask.objects.filter(points__device__site__code=site_code)
            .annotate(last_version=Max("versions__version"))
            .filter(last_version__isnull=False)
            .distinct()
        )
        total_tasks = base_qs.count()
        active_tasks = base_qs.filter(is_active=True).count()

        runs_qs = (
            models.TaskRun.objects.filter(task__in=base_qs)
            .select_related("task", "worker")
            .order_by("-created_at")
        )
        recent_runs = runs_qs.filter(created_at__gte=recent_window)[:20]

        status_counter: dict[str, int] = {}
        for status_value in runs_qs.values_list("status", flat=True):
            status_counter[status_value] = status_counter.get(status_value, 0) + 1

        recent_data = [
            {
                "task": run.task.code,
                "status": run.status,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "worker": run.worker.identifier if run.worker else None,
                "log_reference": run.log_reference,
            }
            for run in recent_runs
        ]

        payload = {
            "total_tasks": total_tasks,
            "active_tasks": active_tasks,
            "status": status_counter,
            "recent_runs": recent_data,
            "generated_at": now,
        }
        serializer = serializers.TaskOverviewSerializer(payload)
        return Response(serializer.data)

    @extend_schema(summary="启动采集任务", request=serializers.TaskControlSerializer)
    @action(detail=True, methods=["post"], url_path="start")
    def start(self, request, pk=None):
        task: models.AcqTask = self.get_object()
        serializer = serializers.TaskControlSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        worker_identifier = serializer.validated_data.get("worker")
        worker = None
        if worker_identifier:
            worker, _ = models.WorkerEndpoint.objects.get_or_create(
                identifier=worker_identifier,
                defaults={"host": worker_identifier},
            )
        run = models.TaskRun.objects.create(
            task=task,
            worker=worker,
            status=models.TaskRun.STATUS_RUNNING,
            started_at=timezone.now(),
            context={"note": serializer.validated_data.get("note")},
        )
        return Response({"detail": "任务已启动", "run_id": run.id})

    @extend_schema(summary="停止采集任务", request=serializers.TaskControlSerializer)
    @action(detail=True, methods=["post"], url_path="stop")
    def stop(self, request, pk=None):
        task: models.AcqTask = self.get_object()
        serializer = serializers.TaskControlSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        run = task.runs.filter(status=models.TaskRun.STATUS_RUNNING).order_by("-created_at").first()
        if not run:
            return Response({"detail": "未找到运行中的任务实例"}, status=status.HTTP_400_BAD_REQUEST)
        run.status = models.TaskRun.STATUS_STOPPED
        run.finished_at = timezone.now()
        context = run.context or {}
        note = serializer.validated_data.get("note")
        if note:
            context["note"] = note
        run.context = context
        run.save(update_fields=["status", "finished_at", "context", "updated_at"])
        return Response({"detail": "任务已停止", "run_id": run.id})


@extend_schema_view(
    list=extend_schema(summary="列出导入作业"),
    retrieve=extend_schema(summary="查看导入作业"),
    create=extend_schema(summary="创建导入作业"),
)
class ImportJobViewSet(
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    """导入作业：上传 Excel 并执行校验/导入。"""

    queryset = models.ImportJob.objects.all().order_by("-created_at")

    def get_serializer_class(self):
        if self.action == "create":
            return serializers.ImportJobCreateSerializer
        return serializers.ImportJobSerializer

    def perform_create(self, serializer):
        job = serializer.save()
        file_path = serializer.saved_path
        result = tasks.process_excel_import.delay(job.id, str(file_path))
        if hasattr(result, "get") and result.ready():
            result.get()
        job.refresh_from_db()
        self._created_job = job

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        job = getattr(self, "_created_job", None)
        output_serializer = serializers.ImportJobSerializer(job, context=self.get_serializer_context())
        headers = self.get_success_headers(output_serializer.data)
        return Response(output_serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    @extend_schema(summary="应用导入作业，写入配置库")
    @action(detail=True, methods=["post"], url_path="apply")
    def apply(self, request, pk=None):
        job = self.get_object()
        if job.status not in (models.ImportJob.STATUS_VALIDATED, models.ImportJob.STATUS_APPLIED):
            return Response({"detail": "导入作业未处于可应用状态"}, status=status.HTTP_400_BAD_REQUEST)

        summary = job.summary or {}
        file_path = summary.get("file_path")
        if not file_path:
            return Response({"detail": "导入作业缺少文件路径信息"}, status=status.HTTP_400_BAD_REQUEST)

        path = Path(file_path)
        if not path.exists():
            return Response({"detail": f"文件不存在: {path}"}, status=status.HTTP_400_BAD_REQUEST)

        site_code = request.data.get("site_code") or summary.get("site_code") or "default"
        created_by = request.data.get("created_by") or job.triggered_by or (request.user.username if request.user.is_authenticated else "system")

        service = ExcelImportService(job, path)
        result = service.apply(site_code=site_code, created_by=created_by)
        return Response({"detail": "配置已写入", "result": result})

    @extend_schema(summary="查看导入差异", responses=serializers.ImportDiffSerializer)
    @action(detail=True, methods=["get"], url_path="diff")
    def diff(self, request, pk=None):
        job = self.get_object()
        summary = job.summary or {}
        file_path = summary.get("file_path")
        if not file_path:
            return Response({"detail": "导入作业缺少文件路径信息"}, status=status.HTTP_400_BAD_REQUEST)
        path = Path(file_path)
        if not path.exists():
            return Response({"detail": f"文件不存在: {path}"}, status=status.HTTP_400_BAD_REQUEST)
        site_code = request.query_params.get("site_code") or summary.get("site_code") or "default"
        service = ExcelImportService(job, path)
        diff = service.compute_diff(site_code=site_code)
        return Response(diff)


@extend_schema_view(
    list=extend_schema(summary="列出配置版本"),
    retrieve=extend_schema(summary="查看配置版本"),
)
class ConfigVersionViewSet(
    mixins.RetrieveModelMixin, mixins.ListModelMixin, viewsets.GenericViewSet
):
    """配置版本：查询任务快照信息。"""

    queryset = models.ConfigVersion.objects.select_related("task").order_by("-created_at")
    serializer_class = serializers.ConfigVersionSerializer
