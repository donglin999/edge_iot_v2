"""ViewSets for acquisition APIs."""
from __future__ import annotations

import logging
import time
from typing import Dict, Any

from django.db import connection, transaction
from django.db.models import Count, Q
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from django.http import HttpResponse
from rest_framework import viewsets

from acquisition import models as acq_models, serializers, tasks
from acquisition.protocols import ProtocolRegistry
from acquisition.services.templates import build_template
from configuration import models as config_models

logger = logging.getLogger(__name__)


def _update_session_locked(session, *, metadata_mutator=None, field_updates=None):
    """M1: re-read an AcquisitionSession under a row lock, mutate, save.

    The acquisition loop writes ``session.metadata`` (live counters such as
    ``last_read_time``) every cycle. A naive read-modify-write in the request
    handler races with that loop and silently clobbers its updates. Here we
    re-fetch the row inside a transaction with ``select_for_update`` so the
    loop's metadata writes and the view's writes are serialized.

    ``select_for_update`` is a real row lock on Postgres and a no-op on SQLite
    (``has_select_for_update`` is False there) — the surrounding
    ``transaction.atomic`` still keeps the read-modify-write consistent.

    Returns the freshly-locked instance with the changes applied.
    """
    update_fields = {"updated_at"}
    qs = acq_models.AcquisitionSession.objects.all()
    if connection.features.has_select_for_update:
        qs = qs.select_for_update()
    with transaction.atomic():
        locked = qs.get(pk=session.pk)
        if metadata_mutator is not None:
            meta = locked.metadata or {}
            metadata_mutator(meta)
            locked.metadata = meta
            update_fields.add("metadata")
        for field, value in (field_updates or {}).items():
            setattr(locked, field, value)
            update_fields.add(field)
        locked.save(update_fields=sorted(update_fields))
    return locked


class ProtocolViewSet(viewsets.ViewSet):
    """Read-only registry of supported acquisition protocols.

    The frontend uses this to render dynamic device/point forms (one form
    layout per protocol). Adding a new protocol class is enough to make it
    appear here — no view code changes required.
    """

    @extend_schema(summary="列出已注册协议", description="返回每个协议的字段 schema,用于前端动态表单")
    def list(self, request):
        return Response(ProtocolRegistry.describe_all())

    @extend_schema(summary="查看单个协议 schema")
    def retrieve(self, request, pk=None):
        try:
            return Response(ProtocolRegistry.describe(pk))
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)

    @extend_schema(
        summary="下载协议 Excel 配置模板",
        description="生成包含必填/可选列、字段注释、示例行的 .xlsx;protocols 查询参数可指定多个协议(逗号分隔),省略则包含全部",
    )
    @action(detail=False, methods=["get"], url_path="template")
    def template(self, request):
        protos_param = request.query_params.get("protocols", "")
        protos = [p.strip() for p in protos_param.split(",") if p.strip()] or None
        try:
            blob = build_template(protos or [])
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        filename = "edge_iot_excel_template.xlsx"
        resp = HttpResponse(
            blob,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        resp["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp


@extend_schema_view(
    list=extend_schema(summary="列出采集会话", description="查询所有采集会话历史"),
    retrieve=extend_schema(summary="查看会话详情", description="获取指定采集会话的详细信息"),
)
class AcquisitionSessionViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """
    采集会话管理 ViewSet

    提供采集任务的启动、停止、状态查询等功能。
    """

    queryset = acq_models.AcquisitionSession.objects.select_related(
        'task', 'worker'
    ).order_by('-created_at')
    serializer_class = serializers.AcquisitionSessionSerializer

    @extend_schema(
        summary="启动采集任务",
        description="同步验证连接并启动采集任务（5秒超时）",
        request=serializers.StartTaskSerializer,
        responses={
            201: serializers.AcquisitionSessionSerializer,
            400: {"description": "请求参数错误、任务已在运行或连接验证失败"},
            504: {"description": "启动超时"},
        }
    )
    @action(detail=False, methods=['post'], url_path='start-task')
    def start_task(self, request):
        """
        启动采集任务（同步验证）

        POST /api/acquisition/sessions/start-task/
        {
            "task_id": 1,
            "worker_identifier": "worker-01",  // 可选
            "config_version_id": 10,           // 可选
            "metadata": {}                     // 可选
        }

        该接口会在5秒内完成以下操作：
        1. 验证设备连接
        2. 检查测点配置
        3. 启动后台采集任务
        4. 返回详细的健康状态报告
        """
        import time
        from acquisition.protocols import ProtocolRegistry
        from collections import defaultdict

        start_time = time.time()
        TIMEOUT = 5.0  # 5秒超时

        serializer = serializers.StartTaskSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        task_id = serializer.validated_data['task_id']
        task = config_models.AcqTask.objects.prefetch_related(
            'points__device',
            'points__template'
        ).get(pk=task_id)

        # 检查是否已有运行中的会话
        active_session = acq_models.AcquisitionSession.objects.filter(
            task=task,
            status=acq_models.AcquisitionSession.STATUS_RUNNING,
        ).first()

        if active_session:
            return Response(
                {
                    "detail": f"任务 {task.code} 已在运行中",
                    "session_id": active_session.id,
                    "status": active_session.status,
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        # 按设备分组测点
        device_groups = defaultdict(lambda: {"device": None, "points": []})
        for point in task.points.all():
            device_id = point.device.id
            if device_groups[device_id]["device"] is None:
                device_groups[device_id]["device"] = point.device

            extras = dict(point.extra or {})
            point_config = {
                **extras,
                "code": point.code,
                "address": point.address,
            }
            device_groups[device_id]["points"].append(point_config)

        # 同步验证所有设备连接和测点
        validation_results = {}
        all_healthy = True
        total_points = 0
        failed_points = []

        for device_id, group in device_groups.items():
            if time.time() - start_time > TIMEOUT:
                return Response(
                    {
                        "detail": "启动验证超时",
                        "timeout": TIMEOUT,
                        "elapsed": time.time() - start_time,
                    },
                    status=status.HTTP_504_GATEWAY_TIMEOUT
                )

            device = group["device"]
            points = group["points"]
            total_points += len(points)

            try:
                # 创建协议实例并验证连接
                device_config = {
                    "source_ip": device.ip_address,
                    "source_port": device.port,
                    "protocol_type": device.protocol,
                    **(device.metadata or {})
                }

                protocol = ProtocolRegistry.create(device.protocol, device_config)
                protocol.connect()

                # 尝试读取测点验证
                try:
                    readings = protocol.read_points(points)
                    successful_points = len(readings)
                    failed_count = len(points) - successful_points

                    if failed_count > 0:
                        all_healthy = False
                        for point in points:
                            if not any(r["code"] == point["code"] for r in readings):
                                failed_points.append({
                                    "device": device.code,
                                    "point": point["code"],
                                    "reason": "无法读取"
                                })

                    validation_results[device.code] = {
                        "status": "healthy" if failed_count == 0 else "partial",
                        "connected": True,
                        "total_points": len(points),
                        "successful_points": successful_points,
                        "failed_points": failed_count,
                    }
                finally:
                    protocol.disconnect()

            except Exception as e:
                all_healthy = False
                validation_results[device.code] = {
                    "status": "error",
                    "connected": False,
                    "error": str(e),
                    "total_points": len(points),
                }
                # 标记所有测点为失败
                for point in points:
                    failed_points.append({
                        "device": device.code,
                        "point": point["code"],
                        "reason": f"设备连接失败: {str(e)}"
                    })

        # 如果所有设备都无法连接，返回错误
        if not any(v.get("connected") for v in validation_results.values()):
            return Response(
                {
                    "detail": "无法连接到任何设备",
                    "validation_results": validation_results,
                    "failed_points": failed_points[:10],  # 最多返回10个失败测点
                },
                status=status.HTTP_400_BAD_REQUEST
            )

        # 获取或创建Worker
        worker = None
        worker_identifier = serializer.validated_data.get('worker_identifier')
        if worker_identifier:
            worker, _ = config_models.WorkerEndpoint.objects.get_or_create(
                identifier=worker_identifier,
                defaults={'host': worker_identifier}
            )

        # 启动Celery后台任务
        config_version_id = serializer.validated_data.get('config_version_id')
        celery_result = tasks.start_acquisition_task.delay(task_id, config_version_id)

        # 等待会话创建
        time.sleep(0.5)
        session = acq_models.AcquisitionSession.objects.filter(
            celery_task_id=celery_result.id
        ).first()

        # 将验证结果写入会话元数据（M1：行锁下 read-modify-write，避免覆盖
        # 采集循环并发写入的实时计数器）。
        if session:
            startup_validation = {
                "timestamp": timezone.now().isoformat(),
                "all_healthy": all_healthy,
                "total_points": total_points,
                "failed_points_count": len(failed_points),
                "device_results": validation_results,
                "elapsed_seconds": time.time() - start_time,
            }
            if failed_points:
                startup_validation["failed_points"] = failed_points[:20]

            def _set_startup_validation(meta):
                meta["startup_validation"] = startup_validation

            session = _update_session_locked(
                session, metadata_mutator=_set_startup_validation
            )

        logger.info(
            f"Started acquisition task {task_id} ({task.code}), "
            f"celery_task_id={celery_result.id}, all_healthy={all_healthy}, "
            f"total_points={total_points}, failed_points={len(failed_points)}"
        )

        # 构建响应
        response_data = {
            "detail": "任务启动成功" if all_healthy else "任务已启动但部分测点异常",
            "session_id": session.id if session else None,
            "celery_task_id": celery_result.id,
            "task_code": task.code,
            "validation": {
                "all_healthy": all_healthy,
                "total_points": total_points,
                "failed_points_count": len(failed_points),
                "device_results": validation_results,
            },
            "elapsed_seconds": round(time.time() - start_time, 2),
        }

        if failed_points:
            response_data["validation"]["failed_points_sample"] = failed_points[:5]

        return Response(response_data, status=status.HTTP_201_CREATED)

    @extend_schema(
        summary="停止采集会话",
        description="停止指定的采集会话",
        request=serializers.StopSessionSerializer,
        responses={
            200: {"description": "停止指令已发送"},
            400: {"description": "会话已停止或不存在"},
        }
    )
    @action(detail=True, methods=['post'], url_path='stop')
    def stop(self, request, pk=None):
        """
        停止采集会话

        POST /api/acquisition/sessions/{id}/stop/
        {
            "reason": "手动停止"  // 可选
        }
        """
        session = self.get_object()

        if session.status in [
            acq_models.AcquisitionSession.STATUS_STOPPED,
            acq_models.AcquisitionSession.STATUS_ERROR,
        ]:
            return Response(
                {"detail": f"会话已处于 {session.status} 状态，无需再次停止"},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = serializers.StopSessionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        reason = serializer.validated_data.get('reason', '')

        # The acquisition loop polls session.status from DB each cycle, so
        # flipping it here is the single source of truth for graceful stop.
        # We don't dispatch stop_acquisition_task because under --pool=solo the
        # running start_acquisition_task occupies the only worker slot, so the
        # stop task would never get consumed.
        # M1: lock the row so the metadata merge doesn't clobber the loop's
        # concurrent writes (last_read_time 等实时计数器).
        stopped_by = request.user.username if request.user.is_authenticated else 'anonymous'

        def _apply_stop(meta):
            if reason:
                meta['stop_reason'] = reason
            meta['stopped_by'] = stopped_by
            meta['stopped_at_client'] = timezone.now().isoformat()

        session = _update_session_locked(
            session,
            metadata_mutator=_apply_stop,
            field_updates={
                "status": acq_models.AcquisitionSession.STATUS_STOPPED,
                "stopped_at": timezone.now(),
            },
        )

        # Best-effort revoke in case the worker is configured with a multi-slot
        # pool; safe no-op when the task isn't running.
        if session.celery_task_id:
            try:
                from celery import current_app
                current_app.control.revoke(session.celery_task_id, terminate=False)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"revoke failed for {session.celery_task_id}: {exc}")

        logger.info(f"Stop signal applied for session {session.id}, reason: {reason}")

        return Response({
            "detail": "停止指令已发送",
            "session_id": session.id,
            "current_status": session.status,
        })

    @extend_schema(summary="暂停采集会话",
                   description="停止当前采集循环但保留 task 关联,resume 会复用同一个任务起新 session")
    @action(detail=True, methods=['post'], url_path='pause')
    def pause(self, request, pk=None):
        """Pause = stop the loop, mark session 'paused', keep history."""
        session = self.get_object()
        if session.status != acq_models.AcquisitionSession.STATUS_RUNNING:
            return Response(
                {"detail": f"只能暂停运行中的会话,当前状态 {session.status}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Flipping status to PAUSED triggers _should_continue() == False on the
        # next loop tick; the worker disconnects cleanly and the celery task
        # exits. We don't issue revoke() — we want the loop's finally block to
        # flush the final batch and update metadata.
        # M1: lock the row so the metadata merge doesn't clobber loop writes.
        paused_by = request.user.username if request.user.is_authenticated else "anonymous"

        def _apply_pause(meta):
            meta["paused_at"] = timezone.now().isoformat()
            meta["paused_by"] = paused_by

        session = _update_session_locked(
            session,
            metadata_mutator=_apply_pause,
            field_updates={
                "status": acq_models.AcquisitionSession.STATUS_PAUSED,
                "stopped_at": timezone.now(),
            },
        )

        logger.info("Pause requested for session %s", session.id)
        return Response({
            "detail": "暂停指令已发送",
            "session_id": session.id,
            "current_status": session.status,
        })

    @extend_schema(summary="恢复采集会话",
                   description="为同一 task 启动一个新的采集 session;旧的 paused session 保留作为历史")
    @action(detail=True, methods=['post'], url_path='resume')
    def resume(self, request, pk=None):
        """Resume by starting a fresh session bound to the same task.

        We don't reuse the paused session because Modbus/MQTT/S7/OPC-UA all
        rebuild their connection state from scratch on connect(); making
        "resume" mean "new session for same task" keeps things simple and
        protocol-agnostic.
        """
        session = self.get_object()
        if session.status != acq_models.AcquisitionSession.STATUS_PAUSED:
            return Response(
                {"detail": f"只能从已暂停状态恢复,当前状态 {session.status}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Block if another session for this task is already running
        active = acq_models.AcquisitionSession.objects.filter(
            task=session.task,
            status=acq_models.AcquisitionSession.STATUS_RUNNING,
        ).first()
        if active:
            return Response(
                {"detail": f"该任务已有运行中的会话 #{active.id}", "session_id": active.id},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Mark old session metadata for traceability (status stays PAUSED).
        # M1: lock the row to avoid clobbering concurrent loop writes.
        resumed_at = timezone.now().isoformat()
        session = _update_session_locked(
            session,
            metadata_mutator=lambda meta: meta.__setitem__("resumed_at", resumed_at),
        )

        # Dispatch a brand-new session via the same start_acquisition_task path.
        # The celery task itself creates the session row.
        celery_result = tasks.start_acquisition_task.delay(session.task_id)
        time.sleep(0.5)
        new_session = acq_models.AcquisitionSession.objects.filter(
            celery_task_id=celery_result.id,
        ).first()
        if new_session is not None:
            previous_session_id = session.id
            new_session = _update_session_locked(
                new_session,
                metadata_mutator=lambda meta: meta.__setitem__(
                    "resumed_from_session", previous_session_id
                ),
            )

        logger.info("Resumed paused session %s as new session %s",
                    session.id, new_session.id if new_session else "?")
        return Response({
            "detail": "已恢复",
            "previous_session_id": session.id,
            "session_id": new_session.id if new_session else None,
            "celery_task_id": celery_result.id,
        }, status=status.HTTP_201_CREATED)

    @extend_schema(
        summary="查询会话状态详情",
        description="获取采集会话的详细状态信息，包括采集数据点统计",
        responses=serializers.SessionStatusSerializer
    )
    @action(detail=True, methods=['get'], url_path='status')
    def get_status(self, request, pk=None):
        """
        查询会话状态详情

        GET /api/acquisition/sessions/{id}/status/
        """
        session = self.get_object()

        # Live counters maintained by the acquisition loop come from
        # session.metadata (updated every SQLITE_METADATA_INTERVAL seconds).
        # The DataPoint table is unused for the InfluxDB pipeline, so reading
        # from it here would always return zero.
        meta = session.metadata or {}
        points_read = int(meta.get("total_points_read", 0))

        last_read_ts = meta.get("last_read_time")
        last_read_time = None
        if last_read_ts:
            try:
                last_read_time = timezone.datetime.fromtimestamp(
                    float(last_read_ts), tz=timezone.utc
                )
            except (TypeError, ValueError):
                last_read_time = None

        # Aggregate error count from device health summary (consecutive failures).
        device_health = meta.get("device_health", {}) or {}
        error_count = sum(
            int(v.get("consecutive_failures", 0))
            for v in device_health.values()
            if isinstance(v, dict)
        )

        # 计算运行时长
        duration_seconds = None
        if session.started_at:
            end_time = session.stopped_at or timezone.now()
            duration_seconds = (end_time - session.started_at).total_seconds()

        status_data = {
            'session_id': session.id,
            'task_code': session.task.code,
            'task_name': session.task.name,
            'status': session.status,
            'celery_task_id': session.celery_task_id,
            'started_at': session.started_at,
            'stopped_at': session.stopped_at,
            'duration_seconds': duration_seconds,
            'points_read': points_read,
            'last_read_time': last_read_time,
            'error_count': error_count,
            'error_message': session.error_message,
            'metadata': meta,
        }

        serializer = serializers.SessionStatusSerializer(status_data)
        return Response(serializer.data)

    @extend_schema(
        summary="查询活跃会话列表",
        description="获取所有运行中和启动中的采集会话",
        responses=serializers.AcquisitionSessionSerializer(many=True)
    )
    @action(detail=False, methods=['get'], url_path='active')
    def active_sessions(self, request):
        """
        查询活跃会话列表

        GET /api/acquisition/sessions/active/
        """
        active_sessions = acq_models.AcquisitionSession.objects.filter(
            status=acq_models.AcquisitionSession.STATUS_RUNNING,
        ).select_related('task', 'worker').order_by('-started_at')

        serializer = self.get_serializer(active_sessions, many=True)
        return Response(serializer.data)

    @extend_schema(
        summary="查询会话的数据点",
        description="获取指定会话采集的数据点列表",
        responses=serializers.DataPointSerializer(many=True)
    )
    @action(detail=True, methods=['get'], url_path='data-points')
    def data_points(self, request, pk=None):
        """
        查询会话的数据点

        GET /api/acquisition/sessions/{id}/data-points/?limit=100&offset=0
        """
        session = self.get_object()

        # 分页参数
        limit = int(request.query_params.get('limit', 100))
        offset = int(request.query_params.get('offset', 0))

        data_points = acq_models.DataPoint.objects.filter(
            session=session
        ).order_by('-timestamp')[offset:offset + limit]

        serializer = serializers.DataPointSerializer(data_points, many=True)
        return Response({
            'count': acq_models.DataPoint.objects.filter(session=session).count(),
            'results': serializer.data,
        })

    @extend_schema(
        summary="查询测点历史数据趋势",
        description="获取指定测点的历史数据，用于绘制趋势图",
        responses={
            200: {
                "description": "历史数据列表",
                "content": {
                    "application/json": {
                        "example": {
                            "point_code": "Temperature_01",
                            "start_time": "2025-10-10T00:00:00Z",
                            "end_time": "2025-10-10T12:00:00Z",
                            "data": [
                                {"timestamp": "2025-10-10T00:00:00Z", "value": 25.5, "quality": "good"},
                                {"timestamp": "2025-10-10T00:01:00Z", "value": 25.6, "quality": "good"}
                            ]
                        }
                    }
                }
            }
        }
    )
    @action(detail=False, methods=['get'], url_path='point-history')
    def point_history(self, request):
        """
        查询测点历史数据 (from InfluxDB)

        GET /api/acquisition/sessions/point-history/?point_code=xxx&start_time=xxx&end_time=xxx&limit=1000
        """
        point_code = request.query_params.get('point_code')
        if not point_code:
            return Response(
                {"detail": "缺少参数: point_code"},
                status=status.HTTP_400_BAD_REQUEST
            )

        start_time = request.query_params.get('start_time', '-1h')
        end_time = request.query_params.get('end_time', 'now()')
        limit = int(request.query_params.get('limit', 1000))

        from django.conf import settings as dj_settings
        from storage import StorageRegistry

        influx_config = {
            "url": getattr(dj_settings, "INFLUXDB_URL", None),
            "host": getattr(dj_settings, "INFLUXDB_HOST", "localhost"),
            "port": getattr(dj_settings, "INFLUXDB_PORT", 8086),
            "token": getattr(dj_settings, "INFLUXDB_TOKEN", ""),
            "org": getattr(dj_settings, "INFLUXDB_ORG", "default"),
            "bucket": getattr(dj_settings, "INFLUXDB_BUCKET", "default"),
        }

        # Build Flux range clause: relative durations stay bare, RFC3339 stays bare,
        # `now()` is a function call. Anything else is escaped as a string literal
        # to prevent Flux injection.
        def _flux_range_arg(value: str) -> str:
            v = value.strip()
            if v == "now()" or v.startswith("-") or v[:1].isdigit():
                return v
            return f'"{v}"'

        # Escape user input to prevent Flux injection in the filter.
        safe_point = point_code.replace("\\", "\\\\").replace('"', '\\"')
        bucket = influx_config["bucket"]
        # M5: point_code is no longer a tag - it is the field *key*. Filter on
        # _field so only the numeric series is returned (cn_name / unit fields
        # are excluded).
        flux_query = (
            f'from(bucket:"{bucket}") '
            f'|> range(start: {_flux_range_arg(start_time)}, stop: {_flux_range_arg(end_time)}) '
            f'|> filter(fn: (r) => r["_field"] == "{safe_point}") '
            f'|> sort(columns: ["_time"]) '
            f'|> limit(n: {limit})'
        )

        storage = None
        try:
            storage = StorageRegistry.create("influxdb", influx_config)
            storage.connect()
            records = storage.query(flux_query)

            data = []
            for record in records:
                ts = record.get("_time")
                value = record.get("_value")
                if ts is None or value is None:
                    continue
                # InfluxDB returns datetime; serialize ISO-8601.
                if hasattr(ts, "isoformat"):
                    ts_str = ts.isoformat()
                else:
                    ts_str = str(ts)

                # Numeric values pass through as-is; non-numeric stays as string.
                if isinstance(value, (int, float)):
                    out_value = value
                else:
                    try:
                        out_value = float(value)
                    except (TypeError, ValueError):
                        out_value = value

                data.append({
                    "timestamp": ts_str,
                    "value": out_value,
                    "quality": record.get("quality", "good"),
                })

            return Response({
                "point_code": point_code,
                "start_time": start_time,
                "end_time": end_time,
                "count": len(data),
                "data": data,
            })

        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to query InfluxDB for point {point_code}: {e}")
            return Response({
                "point_code": point_code,
                "start_time": start_time,
                "end_time": end_time,
                "count": 0,
                "data": [],
                "error": str(e),
            })
        finally:
            if storage is not None:
                try:
                    storage.disconnect()
                except Exception:  # noqa: BLE001
                    pass

    @extend_schema(
        summary="测试单次采集",
        description="执行单次采集测试，不创建持久会话",
        request=serializers.StartTaskSerializer,
        responses={200: {"description": "采集结果"}}
    )
    @action(detail=False, methods=['post'], url_path='test-acquire')
    def test_acquire(self, request):
        """
        测试单次采集

        POST /api/acquisition/sessions/test-acquire/
        {
            "task_id": 1
        }
        """
        serializer = serializers.StartTaskSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        task_id = serializer.validated_data['task_id']

        # 异步执行单次采集
        celery_result = tasks.acquire_once.delay(task_id)

        # 等待结果（最多10秒）
        try:
            result = celery_result.get(timeout=10)
            return Response({
                "detail": "单次采集完成",
                "task_id": task_id,
                "result": result,
            })
        except Exception as e:
            logger.error(f"Test acquisition failed: {e}", exc_info=True)
            return Response({
                "detail": "单次采集失败",
                "error": str(e),
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@extend_schema_view(
    create=extend_schema(
        summary="测试设备连接",
        description="测试与设备的协议连接是否正常"
    ),
)
class ConnectionTestViewSet(
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """设备连接测试 ViewSet"""

    serializer_class = serializers.TestConnectionSerializer

    def create(self, request, *args, **kwargs):
        """
        测试设备连接

        POST /api/acquisition/connection-tests/
        {
            "protocol_type": "modbustcp",
            "device_config": {
                "source_ip": "192.168.1.100",
                "source_port": 502
            }
        }
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        protocol_type = serializer.validated_data['protocol_type']
        device_config = serializer.validated_data['device_config']

        # 异步测试连接
        celery_result = tasks.check_protocol_connection.delay(protocol_type, device_config)

        # 等待结果（最多5秒）
        try:
            result = celery_result.get(timeout=5)
            result_serializer = serializers.ConnectionTestResultSerializer(result)
            return Response(result_serializer.data)
        except Exception as e:
            logger.error(f"Connection test failed: {e}", exc_info=True)
            return Response({
                "status": "error",
                "protocol": protocol_type,
                "error": str(e),
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@extend_schema_view(
    create=extend_schema(
        summary="测试存储连接",
        description="测试与存储后端的连接是否正常"
    ),
)
class StorageTestViewSet(
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """存储连接测试 ViewSet"""

    serializer_class = serializers.TestStorageSerializer

    def create(self, request, *args, **kwargs):
        """
        测试存储连接

        POST /api/acquisition/storage-tests/
        {
            "storage_type": "influxdb",
            "storage_config": {
                "url": "http://localhost:8086",
                "token": "xxx",
                "org": "my-org",
                "bucket": "my-bucket"
            }
        }
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        storage_type = serializer.validated_data['storage_type']
        storage_config = serializer.validated_data['storage_config']

        # 异步测试连接
        celery_result = tasks.check_storage_connection.delay(storage_type, storage_config)

        # 等待结果（最多5秒）
        try:
            result = celery_result.get(timeout=5)
            result_serializer = serializers.ConnectionTestResultSerializer(result)
            return Response(result_serializer.data)
        except Exception as e:
            logger.error(f"Storage test failed: {e}", exc_info=True)
            return Response({
                "status": "error",
                "storage": storage_type,
                "error": str(e),
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ---------------------------------------------------------------------------
# Alarm rules + alarms
# ---------------------------------------------------------------------------


from rest_framework import serializers as drf_serializers


class AlarmRuleSerializer(drf_serializers.ModelSerializer):
    class Meta:
        model = acq_models.AlarmRule
        fields = "__all__"
        read_only_fields = ("id", "created_at", "updated_at")


class AlarmSerializer(drf_serializers.ModelSerializer):
    # ``rule`` is nullable now (connectivity/system alarms have no rule), so
    # ``rule_name`` must tolerate ``rule is None`` — DRF's attribute traversal
    # returns None for a null relation, and ``default=None`` covers the rest.
    # ``severity`` / ``category`` / ``dedup_key`` are real model fields
    # (``fields="__all__"`` picks them up); severity is denormalized onto the
    # row so rule-less alarms carry their own level.
    rule_name = drf_serializers.CharField(
        source="rule.name", read_only=True, default=None, allow_null=True,
    )

    class Meta:
        model = acq_models.Alarm
        fields = "__all__"
        read_only_fields = (
            "id", "fired_at", "created_at", "updated_at", "rule_name",
        )


class AlarmRuleViewSet(viewsets.ModelViewSet):
    """CRUD for alarm rules."""
    queryset = acq_models.AlarmRule.objects.all()
    serializer_class = AlarmRuleSerializer


class AlarmViewSet(mixins.ListModelMixin,
                   mixins.RetrieveModelMixin,
                   viewsets.GenericViewSet):
    """List + acknowledge alarms (acked / cleared transitions)."""
    queryset = acq_models.Alarm.objects.select_related("rule", "session__task")
    serializer_class = AlarmSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)
        return qs

    @action(detail=True, methods=["post"], url_path="ack")
    def acknowledge(self, request, pk=None):
        alarm = self.get_object()
        if alarm.status != acq_models.Alarm.STATUS_FIRING:
            return Response({"detail": "只能确认 firing 状态的告警"},
                            status=status.HTTP_400_BAD_REQUEST)
        alarm.status = acq_models.Alarm.STATUS_ACKED
        alarm.acknowledged_at = timezone.now()
        alarm.acknowledged_by = (request.user.username
                                 if request.user.is_authenticated else "anonymous")
        alarm.save(update_fields=["status", "acknowledged_at",
                                  "acknowledged_by", "updated_at"])
        return Response(AlarmSerializer(alarm).data)
