"""ViewSets for acquisition APIs."""
from __future__ import annotations

import logging
from typing import Dict, Any

from django.db.models import Count, Q
from django.utils import timezone
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from acquisition import models as acq_models, serializers, tasks
from configuration import models as config_models

logger = logging.getLogger(__name__)


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

            point_config = {
                "code": point.code,
                "address": point.address,
                "type": point.extra.get("type", "int16") if point.extra else "int16",
                "num": point.extra.get("num", 1) if point.extra else 1,
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

        # 将验证结果写入会话元数据
        if session:
            session.metadata = session.metadata or {}
            session.metadata["startup_validation"] = {
                "timestamp": timezone.now().isoformat(),
                "all_healthy": all_healthy,
                "total_points": total_points,
                "failed_points_count": len(failed_points),
                "device_results": validation_results,
                "elapsed_seconds": time.time() - start_time,
            }
            if failed_points:
                session.metadata["startup_validation"]["failed_points"] = failed_points[:20]
            session.save(update_fields=['metadata'])

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

        # 发送停止任务
        tasks.stop_acquisition_task.delay(session.id)

        # 记录停止原因
        reason = serializer.validated_data.get('reason', '')
        if reason:
            metadata = session.metadata or {}
            metadata['stop_reason'] = reason
            metadata['stopped_by'] = request.user.username if request.user.is_authenticated else 'anonymous'
            metadata['stopped_at_client'] = timezone.now().isoformat()
            session.metadata = metadata
            session.save(update_fields=['metadata'])

        logger.info(f"Stop signal sent for session {session.id}, reason: {reason}")

        return Response({
            "detail": "停止指令已发送",
            "session_id": session.id,
            "current_status": session.status,
        })

    @extend_schema(
        summary="暂停采集会话（预留）",
        description="暂停指定的采集会话，暂未实现",
        responses={501: {"description": "功能未实现"}}
    )
    @action(detail=True, methods=['post'], url_path='pause')
    def pause(self, request, pk=None):
        """暂停采集会话 - 方案C功能，暂未实现"""
        return Response(
            {"detail": "暂停功能将在方案C中实现"},
            status=status.HTTP_501_NOT_IMPLEMENTED
        )

    @extend_schema(
        summary="恢复采集会话（预留）",
        description="恢复已暂停的采集会话，暂未实现",
        responses={501: {"description": "功能未实现"}}
    )
    @action(detail=True, methods=['post'], url_path='resume')
    def resume(self, request, pk=None):
        """恢复采集会话 - 方案C功能，暂未实现"""
        return Response(
            {"detail": "恢复功能将在方案C中实现"},
            status=status.HTTP_501_NOT_IMPLEMENTED
        )

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

        # 统计数据点
        data_points_count = acq_models.DataPoint.objects.filter(
            session=session
        ).count()

        # 统计错误数据点
        error_count = acq_models.DataPoint.objects.filter(
            session=session,
            quality__in=['bad', 'uncertain']
        ).count()

        # 获取最后一次读取时间
        last_data_point = acq_models.DataPoint.objects.filter(
            session=session
        ).order_by('-timestamp').first()

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
            'points_read': data_points_count,
            'last_read_time': last_data_point.timestamp if last_data_point else None,
            'error_count': error_count,
            'error_message': session.error_message,
            'metadata': session.metadata or {},
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

        # 时间范围参数
        start_time = request.query_params.get('start_time', '-1h')  # Default to last hour
        end_time = request.query_params.get('end_time', 'now()')
        limit = int(request.query_params.get('limit', 1000))

        # Query InfluxDB using Docker CLI (workaround for external auth issue)
        import subprocess

        # Build Flux query
        flux_query = f'from(bucket:"iot-data") |> range(start: {start_time}, stop: {end_time}) |> filter(fn: (r) => r["point"] == "{point_code}") |> limit(n: {limit}) |> sort(columns: ["_time"])'

        try:
            result = subprocess.run(
                ['docker', 'exec', 'influxdb', 'influx', 'query', flux_query, '--raw'],
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                raise Exception(f"Docker command failed: {result.stderr}")

            # Parse CSV output (InfluxDB format with leading commas for data rows)
            data = []
            lines = result.stdout.strip().split('\n')

            # Find header line (starts with ,result,table,...)
            header_idx = -1
            for i, line in enumerate(lines):
                if line.startswith(',result,table,'):
                    header_idx = i
                    break

            if header_idx >= 0 and len(lines) > header_idx + 1:
                headers = lines[header_idx].split(',')
                time_idx = headers.index('_time') if '_time' in headers else -1
                value_idx = headers.index('_value') if '_value' in headers else -1

                for line in lines[header_idx + 1:]:
                    # Data lines start with double comma ,,
                    if not line.startswith(',,') or not line.strip():
                        continue

                    parts = line.split(',')
                    if len(parts) > max(time_idx, value_idx) and time_idx >= 0 and value_idx >= 0:
                        try:
                            # InfluxDB timestamps are in RFC3339 format
                            timestamp = parts[time_idx] if time_idx < len(parts) else ''
                            value = float(parts[value_idx]) if value_idx < len(parts) and parts[value_idx] else 0

                            data.append({
                                'timestamp': timestamp,
                                'value': value,
                                'quality': 'good',
                            })
                        except (ValueError, IndexError) as e:
                            logger.debug(f"Failed to parse line: {line}, error: {e}")
                            continue

            return Response({
                'point_code': point_code,
                'start_time': start_time,
                'end_time': end_time,
                'count': len(data),
                'data': data,
            })

        except Exception as e:
            logger.error(f"Failed to query InfluxDB: {e}")
            return Response({
                'point_code': point_code,
                'start_time': start_time,
                'end_time': end_time,
                'count': 0,
                'data': [],
                'error': str(e),
            })

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
        celery_result = tasks.test_protocol_connection.delay(protocol_type, device_config)

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
        celery_result = tasks.test_storage_connection.delay(storage_type, storage_config)

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
