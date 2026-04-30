
"""ViewSets for configuration APIs."""
from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from django.db import transaction
from django.db.models import Max
from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiParameter
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from configuration.services.exporter import ExcelExportService
from configuration.services.importer import ExcelImportService
from . import models, serializers, tasks

logger = logging.getLogger(__name__)


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

    @extend_schema(summary="获取设备的所有测点", responses=serializers.PointSerializer(many=True))
    @action(detail=True, methods=["get"], url_path="points")
    def list_points(self, request, pk=None):
        """获取设备下的所有测点"""
        device = self.get_object()
        points = device.points.select_related("template", "channel").order_by("code")
        serializer = serializers.PointSerializer(points, many=True, context={"request": request})
        return Response(serializer.data)

    @extend_schema(summary="获取设备统计信息")
    @action(detail=True, methods=["get"], url_path="stats")
    def stats(self, request, pk=None):
        """获取设备统计信息"""
        device = self.get_object()

        # 统计测点数量
        total_points = device.points.count()

        # 统计关联的任务
        related_tasks = models.AcqTask.objects.filter(points__device=device).distinct()
        task_count = related_tasks.count()

        # 查找最近的采集会话
        from acquisition import models as acq_models
        recent_session = acq_models.AcquisitionSession.objects.filter(
            task__points__device=device
        ).order_by("-started_at").first()

        last_acquisition = None
        if recent_session and recent_session.started_at:
            last_acquisition = recent_session.started_at.isoformat()

        stats = {
            "total_points": total_points,
            "task_count": task_count,
            "last_acquisition": last_acquisition,
            "related_tasks": [
                {
                    "id": task.id,
                    "code": task.code,
                    "name": task.name,
                    "is_active": task.is_active,
                }
                for task in related_tasks[:5]  # 最多返回5个
            ]
        }

        return Response(stats)

    @extend_schema(summary="测试设备连接")
    @action(detail=True, methods=["post"], url_path="test-connection")
    def test_connection(self, request, pk=None):
        """测试设备连接（同步本地调用，不走 Celery）。

        连接测试是短交互：开 TCP、发心跳、关 TCP。直接在请求线程里跑可避免
        Celery solo pool 被长跑采集任务独占时无法获取结果的问题。
        """
        device = self.get_object()

        from acquisition.protocols import ProtocolRegistry

        metadata = device.metadata or {}
        device_config = {
            "source_ip": device.ip_address,
            "source_port": device.port or 502,
            "slave_id": metadata.get("slave_id", 1),
            "byte_order": metadata.get("byte_order", "big"),
            "timeout": min(float(metadata.get("timeout", 5.0)), 5.0),
        }

        try:
            protocol = ProtocolRegistry.create(device.protocol, device_config)
            with protocol:
                healthy = protocol.health_check()
                connected = protocol.is_connected

            details = {
                "status": "success" if healthy else "unhealthy",
                "protocol": device.protocol,
                "connected": connected,
                "healthy": healthy,
            }
            return Response({
                "success": healthy,
                "message": "连接测试成功" if healthy else "连接异常",
                "details": details,
            })
        except Exception as e:
            logger.warning("Test connection failed for device %s: %s", device.id, e)
            return Response({
                "success": False,
                "message": f"连接测试失败: {str(e)}",
                "details": {
                    "status": "error",
                    "protocol": device.protocol,
                    "error": str(e),
                },
            })

    @extend_schema(summary="获取设备所有测点的最新值")
    @action(detail=True, methods=["get"], url_path="latest-values")
    def latest_values(self, request, pk=None):
        """
        获取设备所有测点的最新值。

        通过一次 InfluxDB Flux 查询拿到设备 measurement 下每个 _field 的最新值，
        与 Django ORM 中的测点元数据合并返回。
        """
        from datetime import datetime, timezone as dt_timezone

        from django.conf import settings as dj_settings
        from storage import StorageRegistry
        from acquisition import models as acq_models

        device = self.get_object()
        points_qs = device.points.select_related("template", "channel").order_by("code")

        # ------------------------------------------------------------
        # online / last_activity_at: 来自最新 running session.metadata
        # ------------------------------------------------------------
        running_session = (
            acq_models.AcquisitionSession.objects.filter(
                task__points__device=device,
                status=acq_models.AcquisitionSession.STATUS_RUNNING,
            )
            .order_by("-started_at")
            .first()
        )

        last_activity_at = None
        online = False
        if running_session:
            meta = running_session.metadata or {}
            last_read_ts = meta.get("last_read_time")
            if last_read_ts is not None:
                try:
                    last_read_dt = datetime.fromtimestamp(
                        float(last_read_ts), tz=dt_timezone.utc
                    )
                    last_activity_at = last_read_dt.isoformat().replace("+00:00", "Z")
                    age = (timezone.now() - last_read_dt).total_seconds()
                    online = age < 30
                except (TypeError, ValueError):
                    last_activity_at = None
                    online = False

        # ------------------------------------------------------------
        # InfluxDB: 一次查询取每个 _field 的 last()
        # ------------------------------------------------------------
        influx_config = {
            "url": getattr(dj_settings, "INFLUXDB_URL", None),
            "host": getattr(dj_settings, "INFLUXDB_HOST", "localhost"),
            "port": getattr(dj_settings, "INFLUXDB_PORT", 8086),
            "token": getattr(dj_settings, "INFLUXDB_TOKEN", ""),
            "org": getattr(dj_settings, "INFLUXDB_ORG", "default"),
            "bucket": getattr(dj_settings, "INFLUXDB_BUCKET", "default"),
        }

        # device.code 来自数据库，仍要 escape 以防 Flux 注入
        safe_measurement = device.code.replace("\\", "\\\\").replace('"', '\\"')
        bucket = influx_config["bucket"]
        flux_query = (
            f'from(bucket:"{bucket}") '
            f'|> range(start: -1h) '
            f'|> filter(fn: (r) => r["_measurement"] == "{safe_measurement}") '
            f'|> group(columns: ["_field"]) '
            f'|> last()'
        )

        latest_by_field: dict = {}
        storage = None
        try:
            storage = StorageRegistry.create("influxdb", influx_config)
            storage.connect()
            records = storage.query(flux_query)
            for record in records:
                field = record.get("_field")
                if not field:
                    continue
                ts = record.get("_time")
                value = record.get("_value")
                quality = record.get("quality", "good")

                if hasattr(ts, "isoformat"):
                    ts_str = ts.isoformat().replace("+00:00", "Z")
                elif ts is None:
                    ts_str = None
                else:
                    ts_str = str(ts)

                if isinstance(value, (int, float, bool)) or value is None:
                    out_value = value
                else:
                    try:
                        out_value = float(value)
                    except (TypeError, ValueError):
                        out_value = value

                latest_by_field[field] = {
                    "value": out_value,
                    "quality": quality,
                    "timestamp": ts_str,
                }
        except Exception as e:  # noqa: BLE001
            # 容错：Influx 不可用时仍返回测点元信息（value/timestamp = null）
            import logging as _logging
            _logging.getLogger(__name__).warning(
                f"latest-values: InfluxDB query failed for device {device.code}: {e}"
            )
        finally:
            if storage is not None:
                try:
                    storage.disconnect()
                except Exception:  # noqa: BLE001
                    pass

        # ------------------------------------------------------------
        # 合并 ORM 测点信息与 Influx 最新值
        # ------------------------------------------------------------
        points_payload = []
        for point in points_qs:
            template = point.template
            point_name = template.name if template else point.description or point.code
            unit = template.unit if template else ""
            data_type = template.data_type if template else ""

            entry = latest_by_field.get(point.code)
            if entry is None:
                points_payload.append({
                    "point_code": point.code,
                    "point_name": point_name,
                    "unit": unit,
                    "data_type": data_type,
                    "value": None,
                    "quality": "good",
                    "timestamp": None,
                })
            else:
                points_payload.append({
                    "point_code": point.code,
                    "point_name": point_name,
                    "unit": unit,
                    "data_type": data_type,
                    "value": entry["value"],
                    "quality": entry["quality"],
                    "timestamp": entry["timestamp"],
                })

        return Response({
            "device_id": device.id,
            "device_code": device.code,
            "device_name": device.name,
            "protocol": device.protocol,
            "online": online,
            "last_activity_at": last_activity_at,
            "points": points_payload,
        })


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

    @extend_schema(summary="按 task/device/point 任意层级筛选获取测点最新值")
    @action(detail=False, methods=["get"], url_path="latest-values")
    def latest_values(self, request):
        """
        统一的测点最新值端点。支持按 task_id / device_id / point_code 任意组合筛选。
        所有 query 参数都可选；不传 = 不在该层筛选。
        按 device 分组做 InfluxDB 查询（每个 device 一次 Flux 查询）。
        """
        from django.conf import settings as dj_settings
        from storage import StorageRegistry

        task_id = request.query_params.get("task_id") or None
        device_id = request.query_params.get("device_id") or None
        point_code = request.query_params.get("point_code") or None

        # ------------------------------------------------------------
        # ORM filter chain
        # ------------------------------------------------------------
        qs = (
            models.Point.objects.select_related("device", "template", "channel")
            .order_by("device__name", "code")
        )
        if task_id:
            qs = qs.filter(tasks__id=task_id)
        if device_id:
            qs = qs.filter(device_id=device_id)
        if point_code:
            qs = qs.filter(code=point_code)
        if task_id:
            qs = qs.distinct()

        points_list = list(qs)

        # ------------------------------------------------------------
        # group points by device, do one Flux query per device
        # ------------------------------------------------------------
        points_by_device: dict[int, list] = {}
        for p in points_list:
            points_by_device.setdefault(p.device_id, []).append(p)

        influx_config = {
            "url": getattr(dj_settings, "INFLUXDB_URL", None),
            "host": getattr(dj_settings, "INFLUXDB_HOST", "localhost"),
            "port": getattr(dj_settings, "INFLUXDB_PORT", 8086),
            "token": getattr(dj_settings, "INFLUXDB_TOKEN", ""),
            "org": getattr(dj_settings, "INFLUXDB_ORG", "default"),
            "bucket": getattr(dj_settings, "INFLUXDB_BUCKET", "default"),
        }
        bucket = influx_config["bucket"]

        # device_id -> { field_code -> {value, quality, timestamp} }
        latest_by_device: dict[int, dict] = {}

        if points_by_device:
            storage = None
            try:
                storage = StorageRegistry.create("influxdb", influx_config)
                storage.connect()
                for dev_id, dev_points in points_by_device.items():
                    device = dev_points[0].device
                    safe_measurement = device.code.replace("\\", "\\\\").replace('"', '\\"')
                    flux_query = (
                        f'from(bucket:"{bucket}") '
                        f'|> range(start: -1h) '
                        f'|> filter(fn: (r) => r["_measurement"] == "{safe_measurement}") '
                        f'|> group(columns: ["_field"]) '
                        f'|> last()'
                    )
                    field_map: dict = {}
                    try:
                        records = storage.query(flux_query)
                        for record in records:
                            field = record.get("_field")
                            if not field:
                                continue
                            ts = record.get("_time")
                            value = record.get("_value")
                            quality = record.get("quality", "good")

                            if hasattr(ts, "isoformat"):
                                ts_str = ts.isoformat().replace("+00:00", "Z")
                            elif ts is None:
                                ts_str = None
                            else:
                                ts_str = str(ts)

                            if isinstance(value, (int, float, bool)) or value is None:
                                out_value = value
                            else:
                                try:
                                    out_value = float(value)
                                except (TypeError, ValueError):
                                    out_value = value

                            field_map[field] = {
                                "value": out_value,
                                "quality": quality,
                                "timestamp": ts_str,
                            }
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "latest-values: InfluxDB query failed for device %s: %s",
                            device.code, e,
                        )
                    latest_by_device[dev_id] = field_map
            except Exception as e:  # noqa: BLE001
                logger.warning("latest-values: InfluxDB unavailable: %s", e)
            finally:
                if storage is not None:
                    try:
                        storage.disconnect()
                    except Exception:  # noqa: BLE001
                        pass

        # ------------------------------------------------------------
        # merge ORM points with latest values
        # ------------------------------------------------------------
        points_payload = []
        for point in points_list:
            template = point.template
            extra = point.extra or {}
            point_name = template.name if template else (point.description or point.code)
            unit = template.unit if template else (extra.get("unit") or "")
            data_type = template.data_type if template else (extra.get("data_type") or "")

            field_map = latest_by_device.get(point.device_id, {})
            entry = field_map.get(point.code)
            if entry is None:
                value = None
                quality = "good"
                timestamp = None
            else:
                value = entry["value"]
                quality = entry["quality"]
                timestamp = entry["timestamp"]

            points_payload.append({
                "point_code": point.code,
                "point_name": point_name,
                "unit": unit,
                "data_type": data_type,
                "device_id": point.device_id,
                "device_name": point.device.name,
                "value": value,
                "quality": quality,
                "timestamp": timestamp,
            })

        return Response({
            "filter": {
                "task_id": int(task_id) if task_id else None,
                "device_id": int(device_id) if device_id else None,
                "point_code": point_code,
            },
            "count": len(points_payload),
            "points": points_payload,
        })


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

        # 检查是否已有运行中的会话（使用acquisition模块的模型）
        from acquisition import models as acq_models
        active_session = acq_models.AcquisitionSession.objects.filter(
            task=task,
            status__in=[
                acq_models.AcquisitionSession.STATUS_RUNNING,
                acq_models.AcquisitionSession.STATUS_RUNNING,
            ]
        ).first()

        if active_session:
            return Response(
                {
                    "detail": f"任务 {task.code} 已在运行中",
                    "session_id": active_session.id,
                    "status": active_session.status,
                    "started_at": active_session.started_at,
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        # 启动Celery任务
        from acquisition import tasks as acq_tasks
        celery_result = acq_tasks.start_acquisition_task.delay(task.id)

        # 创建TaskRun记录（兼容旧系统）
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
            context={
                "note": serializer.validated_data.get("note"),
                "celery_task_id": celery_result.id,
            },
        )

        return Response({
            "detail": "任务已启动",
            "run_id": run.id,
            "celery_task_id": celery_result.id,
            "message": "请通过 /api/acquisition/sessions/active/ 查询会话状态"
        })

    @extend_schema(summary="停止采集任务", request=serializers.TaskControlSerializer)
    @action(detail=True, methods=["post"], url_path="stop")
    def stop(self, request, pk=None):
        task: models.AcqTask = self.get_object()
        serializer = serializers.TaskControlSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        # 查找运行中的会话（使用acquisition模块的模型）
        from acquisition import models as acq_models, tasks as acq_tasks
        active_session = acq_models.AcquisitionSession.objects.filter(
            task=task,
            status__in=[
                acq_models.AcquisitionSession.STATUS_RUNNING,
                acq_models.AcquisitionSession.STATUS_RUNNING,
            ]
        ).order_by("-started_at").first()

        if not active_session:
            return Response(
                {"detail": "未找到运行中的会话"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 发送停止Celery任务
        acq_tasks.stop_acquisition_task.delay(active_session.id)

        # 更新TaskRun记录（兼容旧系统）
        run = task.runs.filter(status=models.TaskRun.STATUS_RUNNING).order_by("-created_at").first()
        if run:
            run.status = models.TaskRun.STATUS_STOPPED
            run.finished_at = timezone.now()
            context = run.context or {}
            note = serializer.validated_data.get("note")
            if note:
                context["note"] = note
            context["stopped_via_api"] = True
            run.context = context
            run.save(update_fields=["status", "finished_at", "context", "updated_at"])

        return Response({
            "detail": "停止指令已发送",
            "session_id": active_session.id,
            "run_id": run.id if run else None,
        })


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
        mode = request.data.get("mode", "merge")  # Default to merge mode

        # Validate mode
        if mode not in ("replace", "merge", "append"):
            return Response(
                {"detail": f"无效的导入模式: {mode}，必须是 replace、merge 或 append"},
                status=status.HTTP_400_BAD_REQUEST
            )

        service = ExcelImportService(job, path)
        result = service.apply(site_code=site_code, created_by=created_by, mode=mode)
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
    destroy=extend_schema(summary="删除配置版本"),
)
class ConfigVersionViewSet(
    mixins.RetrieveModelMixin,
    mixins.ListModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """配置版本：查询任务快照信息。"""

    queryset = models.ConfigVersion.objects.select_related("task").order_by("-created_at")
    serializer_class = serializers.ConfigVersionSerializer

    def get_queryset(self):
        """Filter by task_id if provided."""
        queryset = super().get_queryset()
        task_id = self.request.query_params.get("task_id")
        if task_id:
            queryset = queryset.filter(task_id=task_id)
        return queryset

    # ------------------------------------------------------------------
    # delete guards (single + bulk)
    # ------------------------------------------------------------------
    def _check_can_delete(self, version: models.ConfigVersion) -> str | None:
        """Return a Chinese rejection reason, or None if deletion is allowed."""
        # Don't strand a task with zero versions.
        if models.ConfigVersion.objects.filter(task_id=version.task_id).count() <= 1:
            return "任务至少保留 1 个版本"

        from acquisition import models as acq_models
        running = acq_models.AcquisitionSession.objects.filter(
            task_id=version.task_id,
            status=acq_models.AcquisitionSession.STATUS_RUNNING,
        ).exists()
        if running:
            return "任务运行中，不可删除版本"
        return None

    def destroy(self, request, *args, **kwargs):
        version = self.get_object()
        reason = self._check_can_delete(version)
        if reason is not None:
            return Response({"detail": reason}, status=status.HTTP_400_BAD_REQUEST)
        return super().destroy(request, *args, **kwargs)

    @extend_schema(summary="批量删除配置版本")
    @action(detail=False, methods=["post"], url_path="bulk-delete")
    def bulk_delete(self, request):
        ids = request.data.get("ids") or []
        if not isinstance(ids, list):
            return Response(
                {"detail": "ids 必须是一个数组"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            ids = [int(v) for v in ids]
        except (TypeError, ValueError):
            return Response(
                {"detail": "ids 必须为整数列表"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        deleted: list[int] = []
        skipped: list[dict] = []

        with transaction.atomic():
            versions = list(
                models.ConfigVersion.objects.select_for_update().filter(id__in=ids)
            )
            found_ids = {v.id for v in versions}
            for missing_id in ids:
                if missing_id not in found_ids:
                    skipped.append({"id": missing_id, "reason": "版本不存在"})

            for version in versions:
                # Capture PK first — Django nulls model.pk after delete().
                version_id = version.id
                reason = self._check_can_delete(version)
                if reason is not None:
                    skipped.append({"id": version_id, "reason": reason})
                    continue
                version.delete()
                deleted.append(version_id)

        return Response({"deleted": deleted, "skipped": skipped})

    # ------------------------------------------------------------------
    # single-version Excel export
    # ------------------------------------------------------------------
    @extend_schema(summary="导出指定版本配置为 Excel")
    @action(detail=True, methods=["get"], url_path="export-excel")
    def export_excel(self, request, pk=None):
        version = self.get_object()
        content = ExcelExportService().export_version(version)
        ts = timezone.now().strftime("%Y%m%d_%H%M%S")
        task_code = version.task.code if version.task else "task"
        # Sanitise filename — task.code can contain spaces/slashes via importer.
        safe_task = task_code.replace("/", "_").replace(" ", "_")
        filename = f"version_{safe_task}_v{version.version}_{ts}.xlsx"
        response = HttpResponse(
            content,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    @extend_schema(summary="回滚到指定配置版本")
    @action(detail=True, methods=["post"], url_path="rollback")
    def rollback(self, request, pk=None):
        """回滚到指定版本的配置."""
        version = self.get_object()
        task = version.task

        # 检查任务是否在运行中
        from acquisition import models as acq_models
        active_session = acq_models.AcquisitionSession.objects.filter(
            task=task,
            status__in=[
                acq_models.AcquisitionSession.STATUS_RUNNING,
                acq_models.AcquisitionSession.STATUS_RUNNING,
            ]
        ).first()

        if active_session:
            return Response(
                {"detail": f"任务 {task.code} 正在运行中，无法回滚配置。请先停止任务。"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # 创建新版本作为回滚版本
        latest = task.versions.order_by("-version").first()
        next_version = (latest.version if latest else 0) + 1

        created_by = (
            request.user.username if request.user.is_authenticated
            else request.data.get("created_by", "system")
        )

        new_version = models.ConfigVersion.objects.create(
            task=task,
            version=next_version,
            summary=f"回滚到版本 {version.version}",
            created_by=created_by,
            payload=version.payload,  # Copy payload from the target version
        )

        return Response({
            "detail": f"已回滚到版本 {version.version}",
            "new_version_id": new_version.id,
            "new_version_number": new_version.version,
            "rollback_from_version": version.version,
        })


class ConfigExportView(APIView):
    """GET /api/config/export-excel/?site_code=<code>

    Streams the live device/point configuration as an .xlsx whose layout
    matches the importer template, so the file round-trips through
    ``/import-jobs/`` without further massaging.
    """

    @extend_schema(
        summary="导出当前配置为 Excel",
        parameters=[
            OpenApiParameter(
                name="site_code",
                description="站点 code，缺省导出全部站点",
                required=False,
                type=str,
            ),
        ],
    )
    def get(self, request):
        site_code = request.query_params.get("site_code") or None
        content = ExcelExportService().export_current_config(site_code=site_code)
        ts = timezone.now().strftime("%Y%m%d_%H%M%S")
        site_label = site_code or "all"
        safe_site = site_label.replace("/", "_").replace(" ", "_")
        filename = f"config_{safe_site}_{ts}.xlsx"
        response = HttpResponse(
            content,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response
