# 数据采集系统完整流程与协议开发指南

## 目录

1. [系统完整数据流](#系统完整数据流)
2. [Excel导入流程](#excel导入流程)
3. [任务发布与执行流程](#任务发布与执行流程)
4. [数据采集流程](#数据采集流程)
5. [前端展示流程](#前端展示流程)
6. [新协议开发指南](#新协议开发指南)

---

## 系统完整数据流

```
┌─────────────────────────────────────────────────────────────────────┐
│                          1. Excel导入阶段                             │
│                                                                       │
│  Excel文件 ──▶ API上传 ──▶ Celery异步解析 ──▶ 写入SQLite3            │
│                                                                       │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                          2. 任务配置阶段                              │
│                                                                       │
│  设备配置 + 测点配置 + 任务配置 ──▶ ConfigVersion快照                 │
│                                                                       │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        3. 任务启动与验证阶段                           │
│                                                                       │
│  前端点击启动 ──▶ Django API同步验证(5秒) ──▶ Celery后台任务          │
│                                                                       │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                          4. 数据采集阶段                              │
│                                                                       │
│  持久连接 ──▶ 批量读取 ──▶ 批量写入InfluxDB ──▶ 健康监控              │
│                                                                       │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                          5. 前端展示阶段                              │
│                                                                       │
│  轮询API ──▶ 显示采集状态 ──▶ 查询历史数据 ──▶ 图表可视化             │
│                                                                       │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Excel导入流程

### 流程图

```
用户上传Excel
    ↓
前端: ImportJobPage.tsx
    ↓ API调用
Django: POST /api/config/import/excel/
    ↓ 创建ImportJob
Celery: process_excel_import.delay(job_id)
    ↓ 后台异步处理
ExcelImporter.import_excel()
    ↓ 解析数据
创建/更新数据库记录
    ├─ Site
    ├─ Device
    ├─ PointTemplate
    ├─ Point
    └─ AcqTask
    ↓
创建ConfigVersion快照
    ↓
更新ImportJob状态
```

### 关键代码快照

#### 1. 前端上传 (frontend/src/pages/ImportJobPage.tsx)

```typescript
const handleFileUpload = async (file: File) => {
  const formData = new FormData();
  formData.append('file', file);

  try {
    const response = await fetch('http://localhost:8000/api/config/import/excel/', {
      method: 'POST',
      body: formData,
    });

    const data = await response.json();
    console.log('导入任务ID:', data.job_id);

    // 轮询检查导入状态
    pollJobStatus(data.job_id);
  } catch (error) {
    console.error('上传失败:', error);
  }
};
```

#### 2. API接收 (backend/configuration/views.py:241-287)

```python
@action(detail=False, methods=["post"], url_path="import/excel")
def import_excel(self, request):
    """
    上传Excel并创建导入任务

    Request:
        - file: Excel文件 (multipart/form-data)

    Response:
        - job_id: 导入任务ID
        - status: 任务状态
    """
    uploaded_file = request.FILES.get("file")
    if not uploaded_file:
        return Response({"detail": "未提供文件"}, status=400)

    # 保存文件
    timestamp = timezone.now().strftime("%Y%m%d_%H%M%S")
    filename = f"import_{timestamp}_{uploaded_file.name}"
    file_path = Path(settings.MEDIA_ROOT) / "uploads" / filename
    file_path.parent.mkdir(parents=True, exist_ok=True)

    with open(file_path, "wb+") as destination:
        for chunk in uploaded_file.chunks():
            destination.write(chunk)

    # 创建导入任务记录
    job = models.ImportJob.objects.create(
        source_name=uploaded_file.name,
        triggered_by=request.user.username if request.user.is_authenticated else "anonymous",
        status=models.ImportJob.STATUS_PENDING,
    )

    # 异步处理
    from configuration import tasks
    tasks.process_excel_import.delay(job.id, str(file_path))

    return Response({
        "job_id": job.id,
        "status": job.status,
        "message": "导入任务已创建，正在后台处理"
    }, status=202)
```

#### 3. Celery异步处理 (backend/configuration/tasks.py:13-65)

```python
@shared_task(bind=True)
def process_excel_import(self, job_id: int, file_path: str):
    """
    异步处理Excel导入

    Args:
        job_id: ImportJob ID
        file_path: Excel文件路径
    """
    logger = logging.getLogger(__name__)
    job = models.ImportJob.objects.get(id=job_id)

    try:
        # 更新状态为处理中
        job.status = models.ImportJob.STATUS_VALIDATED
        job.save()

        # 导入数据
        from configuration.services.importer import ExcelImporter
        importer = ExcelImporter(file_path)
        result = importer.import_excel()

        # 创建配置版本
        if result.get("task"):
            version = models.ConfigVersion.objects.create(
                task=result["task"],
                version=f"v{timezone.now().strftime('%Y%m%d_%H%M%S')}",
                created_by="system",
                snapshot=result.get("snapshot", {}),
            )
            job.related_version = version

        # 更新为成功
        job.status = models.ImportJob.STATUS_APPLIED
        job.summary = result.get("summary", {})
        job.save()

        logger.info(f"Import job {job_id} completed: {job.summary}")

    except Exception as e:
        logger.error(f"Import job {job_id} failed: {e}", exc_info=True)
        job.status = models.ImportJob.STATUS_FAILED
        job.summary = {"error": str(e)}
        job.save()
        raise
```

#### 4. Excel解析器 (backend/configuration/services/importer.py:55-380)

```python
class ExcelImporter:
    """Excel配置文件导入器"""

    def import_excel(self, mode: str = "replace") -> Dict[str, Any]:
        """
        导入Excel配置文件

        Args:
            mode: 导入模式 ("replace"替换 或 "append"追加)

        Returns:
            导入结果摘要
        """
        # 1. 读取Excel文件
        df = pd.read_excel(self.file_path)

        # 2. 解析站点信息
        site = self._parse_site(df)

        # 3. 解析设备信息
        devices = self._parse_devices(df, site)

        # 4. 解析测点信息
        points_created, points_updated = self._parse_points(df, devices, mode)

        # 5. 创建/更新采集任务
        task = self._create_or_update_task(df, site, mode)

        # 6. 关联任务和测点
        self._link_task_points(task, df)

        return {
            "task": task,
            "summary": {
                "site": site.code,
                "devices": len(devices),
                "points_created": points_created,
                "points_updated": points_updated,
                "task": task.code,
            },
            "snapshot": self._create_snapshot(task),
        }

    def _parse_devices(self, df: pd.DataFrame, site: Site) -> Dict[str, Device]:
        """解析设备配置"""
        devices = {}

        for _, row in df.iterrows():
            device_code = self._clean_value(row.get("device_a_tag"))
            if not device_code or device_code in devices:
                continue

            # 提取设备配置
            protocol = self._clean_value(row.get("protocol", "modbus_tcp"))
            ip_address = self._clean_value(row.get("ip"))
            port = int(row.get("port", 502))

            # 创建或更新设备
            device, created = Device.objects.update_or_create(
                site=site,
                code=device_code,
                defaults={
                    "name": self._clean_value(row.get("device_name", device_code)),
                    "protocol": protocol,
                    "ip_address": ip_address,
                    "port": port,
                    "metadata": {
                        "slave_id": int(row.get("slave_id", 1)),
                    },
                },
            )

            devices[device_code] = device

        return devices

    def _parse_points(self, df: pd.DataFrame, devices: Dict, mode: str) -> Tuple[int, int]:
        """解析测点配置"""
        points_created = 0
        points_updated = 0

        for _, row in df.iterrows():
            device_code = self._clean_value(row.get("device_a_tag"))
            point_code = self._clean_value(row.get("point_a_tag"))

            device = devices.get(device_code)
            if not device or not point_code:
                continue

            # 解析测点模板
            template = self._get_or_create_template(row)

            # 构建测点配置
            point_defaults = {
                "template": template,
                "address": str(row.get("source_addr", "")).strip(),
                "description": self._clean_value(row.get("cn_name")),
                "sample_rate_hz": float(row.get("fs", 1.0)),
                "extra": {
                    "type": self._clean_value(row.get("type")),
                    "num": self._clean_value(row.get("num")),
                },
            }

            # 创建或更新测点
            if mode == "append":
                point, created = Point.objects.get_or_create(
                    device=device,
                    code=point_code,
                    defaults=point_defaults,
                )
            else:  # replace
                point, created = Point.objects.update_or_create(
                    device=device,
                    code=point_code,
                    defaults=point_defaults,
                )

            if created:
                points_created += 1
            else:
                points_updated += 1

        return points_created, points_updated
```

---

## 任务发布与执行流程

### 流程图

```
前端点击"启动任务"
    ↓
React: TaskControlPanel.tsx
    ↓ API调用
Django: POST /api/acquisition/sessions/start-task/
    ↓ 同步验证 (5秒超时)
验证设备连接
    ├─ 创建Protocol实例
    ├─ 连接设备
    ├─ 测试读取测点
    └─ 返回验证结果
    ↓ 验证通过
创建AcquisitionSession (status=running)
    ↓ 异步启动
Celery: start_acquisition_task.delay(task_id)
    ↓
AcquisitionService.run_continuous()
    ↓
持续采集循环
```

### 关键代码快照

#### 1. 前端启动任务 (frontend/src/components/acquisition/TaskControlPanel.tsx)

```typescript
const handleStart = async () => {
  setIsLoading(true);

  try {
    const response = await fetch(
      'http://localhost:8000/api/acquisition/sessions/start-task/',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_id: task.id }),
      }
    );

    const data = await response.json();

    if (response.ok) {
      console.log('启动成功:', data.validation);
      // 更新UI显示验证结果
      if (data.validation.all_healthy) {
        showSuccess('任务启动成功，所有测点正常');
      } else {
        showWarning(`任务已启动，但有 ${data.validation.failed_points_count} 个测点异常`);
      }

      // 刷新任务状态
      onRefresh();
    } else {
      showError(data.detail || '启动失败');
    }
  } catch (error) {
    console.error('启动任务失败:', error);
    showError('网络错误');
  } finally {
    setIsLoading(false);
  }
};
```

#### 2. Django同步验证 (backend/acquisition/views.py:40-260)

```python
@action(detail=False, methods=['post'], url_path='start-task')
def start_task(self, request):
    """
    启动采集任务（同步验证）

    该接口会在5秒内完成以下操作：
    1. 验证设备连接
    2. 检查测点配置
    3. 启动后台采集任务
    4. 返回详细的健康状态报告
    """
    start_time = time.time()
    TIMEOUT = 5.0  # 5秒超时

    task_id = request.data.get("task_id")
    if not task_id:
        return Response({"detail": "缺少 task_id 参数"}, status=400)

    try:
        task = config_models.AcqTask.objects.get(id=task_id)
    except config_models.AcqTask.DoesNotExist:
        return Response({"task_id": [f"任务ID {task_id} 不存在"]}, status=404)

    # 检查是否已在运行
    active_session = acq_models.AcquisitionSession.objects.filter(
        task=task,
        status=acq_models.AcquisitionSession.STATUS_RUNNING,
    ).first()

    if active_session:
        return Response({
            "detail": f"任务 {task.code} 已在运行中",
            "session_id": active_session.id,
            "status": active_session.status,
        }, status=409)

    # 获取配置快照
    config_version = task.config_versions.order_by("-created_at").first()
    if not config_version:
        return Response({"detail": "任务没有有效的配置版本"}, status=400)

    # 按设备分组测点
    device_groups = defaultdict(lambda: {"device": None, "points": []})
    for task_point in task.task_points.select_related("point__device", "point__template"):
        point = task_point.point
        device_id = point.device.id

        if device_groups[device_id]["device"] is None:
            device_groups[device_id]["device"] = point.device

        device_groups[device_id]["points"].append({
            "code": point.code,
            "address": point.address,
            "type": point.extra.get("type", "int16") if point.extra else "int16",
            "num": point.extra.get("num", 1) if point.extra else 1,
        })

    # 同步验证所有设备连接和测点
    validation_results = {}
    all_healthy = True
    total_points = 0
    failed_points = []

    for device_id, group in device_groups.items():
        device = group["device"]
        points = group["points"]
        total_points += len(points)

        # 检查超时
        if time.time() - start_time > TIMEOUT:
            return Response({
                "detail": "启动验证超时",
                "elapsed_seconds": round(time.time() - start_time, 2),
            }, status=504)

        try:
            # 创建协议实例并连接
            from acquisition.protocols import ProtocolRegistry

            device_config = {
                "host": device.ip_address,
                "port": device.port,
                "slave_id": device.metadata.get("slave_id", 1) if device.metadata else 1,
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
                    failed_points.extend([
                        f"{device.code}:{p['code']}"
                        for p in points[successful_points:]
                    ])

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
            failed_points.extend([f"{device.code}:{p['code']}" for p in points])

    # 启动Celery后台任务
    from acquisition import tasks as acq_tasks
    celery_result = acq_tasks.start_acquisition_task.delay(task.id, config_version.id)

    return Response({
        "detail": "任务启动成功" if all_healthy else "任务已启动但部分测点异常",
        "session_id": None,  # Session在Celery任务中创建
        "celery_task_id": celery_result.id,
        "validation": {
            "all_healthy": all_healthy,
            "total_points": total_points,
            "failed_points_count": len(failed_points),
            "failed_points": failed_points[:10],  # 最多返回10个失败点
            "device_results": validation_results,
        },
        "elapsed_seconds": round(time.time() - start_time, 2),
    }, status=201)
```

#### 3. Celery后台任务 (backend/acquisition/tasks.py:16-94)

```python
@shared_task(bind=True)
def start_acquisition_task(self, task_id: int, config_version_id: int):
    """
    启动数据采集任务（Celery后台任务）

    Args:
        task_id: 采集任务ID
        config_version_id: 配置版本ID
    """
    logger = logging.getLogger(__name__)

    try:
        task = config_models.AcqTask.objects.get(id=task_id)
        config_version = config_models.ConfigVersion.objects.get(id=config_version_id)

        # 创建采集会话（直接设置为RUNNING状态）
        session = acq_models.AcquisitionSession.objects.create(
            task=task,
            status=acq_models.AcquisitionSession.STATUS_RUNNING,
            celery_task_id=self.request.id,
            started_at=timezone.now(),
        )

        logger.info(f"Acquisition session {session.id} created for task {task.code}")

        # 创建采集服务实例
        from acquisition.services.acquisition_service import AcquisitionService

        service = AcquisitionService(
            task=task,
            config_version=config_version,
            session=session,
        )

        # 运行持续采集
        result = service.run_continuous()

        # 更新会话状态
        session.status = acq_models.AcquisitionSession.STATUS_STOPPED
        session.stopped_at = timezone.now()
        session.metadata = result
        session.save()

        logger.info(f"Acquisition session {session.id} completed: {result}")

        return {
            "session_id": session.id,
            "status": "completed",
            "result": result,
        }

    except Exception as e:
        logger.error(f"Acquisition task failed: {e}", exc_info=True)

        # 更新会话为错误状态
        if 'session' in locals():
            session.status = acq_models.AcquisitionSession.STATUS_ERROR
            session.stopped_at = timezone.now()
            session.error_message = str(e)
            session.save()

        raise
```

---

## 数据采集流程

### 流程图

```
AcquisitionService.run_continuous()
    ↓
初始化所有设备的持久连接
    ├─ 创建Protocol实例
    ├─ protocol.connect()
    └─ 记录设备健康状态
    ↓
进入采集循环 (while status == RUNNING)
    ↓
遍历每个设备
    ├─ 检查连接状态
    ├─ protocol.read_points(批量读取)
    ├─ 添加到批量缓冲区
    └─ 更新设备健康状态
    ↓
检查批量写入条件
    ├─ 缓冲区 >= 50点？
    └─ 或经过 >= 5秒？
    ↓ YES
写入InfluxDB (批量)
    ↓
清空缓冲区
    ↓
检测设备超时 (30秒无响应)
    ├─ 标记为timeout
    ├─ 断开连接
    └─ 触发自动重连 (最多3次)
    ↓
更新Session元数据
    ├─ device_health
    ├─ total_readings
    └─ errors
    ↓
返回循环顶部
```

### 关键代码快照

#### 1. 持久连接与批量采集 (backend/acquisition/services/acquisition_service.py:159-343)

```python
def run_continuous(self) -> Dict[str, Any]:
    """
    运行持久连接的数据采集循环

    核心特性:
    1. 持久连接: 保持设备连接，避免频繁建立/断开
    2. 批量上传: 缓冲50点或5秒后批量写入
    3. 健康监控: 实时监控设备状态，超时检测
    4. 自动重连: 连接失败时自动重试，最多3次

    Returns:
        采集结果摘要
    """
    self.logger.info(f"Starting continuous acquisition for task {self.task.code}")

    # 配置参数
    batch_size = getattr(settings, "ACQUISITION_BATCH_SIZE", 50)
    batch_timeout = getattr(settings, "ACQUISITION_BATCH_TIMEOUT", 5.0)
    connection_timeout = getattr(settings, "ACQUISITION_CONNECTION_TIMEOUT", 30.0)
    max_reconnect_attempts = getattr(settings, "ACQUISITION_MAX_RECONNECT_ATTEMPTS", 3)

    # 初始化持久连接
    device_protocols = {}  # device_id -> protocol instance
    device_health = {}     # device_id -> health status

    # 批量缓冲区
    batch_buffer = []
    batch_start_time = time.time()

    # 统计信息
    total_readings = 0
    errors = []

    try:
        # === 阶段1: 建立所有设备的持久连接 ===
        self.logger.info("Establishing persistent connections...")

        for device_id, group in self.device_groups.items():
            device = group["device"]

            try:
                # 创建协议实例
                device_config = {
                    "host": device.ip_address,
                    "port": device.port,
                    "slave_id": device.metadata.get("slave_id", 1) if device.metadata else 1,
                }

                protocol = ProtocolRegistry.create(device.protocol, device_config)

                # 连接设备 (只连接一次)
                protocol.connect()

                device_protocols[device_id] = protocol
                device_health[device_id] = {
                    "last_success": time.time(),
                    "consecutive_failures": 0,
                    "status": "healthy",
                }

                self.logger.info(f"Connected to device {device.code}")

            except Exception as e:
                self.logger.error(f"Failed to connect to device {device.code}: {e}")
                device_protocols[device_id] = None
                device_health[device_id] = {
                    "last_success": None,
                    "consecutive_failures": 0,
                    "status": "disconnected",
                }

        # === 阶段2: 持续采集循环 ===
        self.logger.info("Entering continuous acquisition loop...")

        while self._should_continue():
            cycle_start = time.time()

            # 遍历所有设备采集数据
            for device_id, group in self.device_groups.items():
                device = group["device"]
                points = group["points"]
                protocol = device_protocols.get(device_id)

                # 检查连接状态
                if protocol is None:
                    # 尝试重连
                    if device_health[device_id]["consecutive_failures"] < max_reconnect_attempts:
                        try:
                            device_config = {
                                "host": device.ip_address,
                                "port": device.port,
                                "slave_id": device.metadata.get("slave_id", 1) if device.metadata else 1,
                            }
                            protocol = ProtocolRegistry.create(device.protocol, device_config)
                            protocol.connect()
                            device_protocols[device_id] = protocol
                            device_health[device_id]["status"] = "healthy"
                            self.logger.info(f"Reconnected to device {device.code}")
                        except Exception as e:
                            device_health[device_id]["consecutive_failures"] += 1
                            self.logger.warning(f"Reconnect failed for {device.code}: {e}")
                            continue
                    else:
                        continue

                try:
                    # === 批量读取测点数据（不断开连接）===
                    readings = protocol.read_points(points)
                    formatted_data = self._format_for_storage(readings, device)
                    batch_buffer.extend(formatted_data)

                    # 更新健康状态
                    device_health[device_id]["last_success"] = time.time()
                    device_health[device_id]["consecutive_failures"] = 0
                    device_health[device_id]["status"] = "healthy"

                    total_readings += len(readings)

                except Exception as e:
                    error_msg = f"Failed to read from device {device.code}: {e}"
                    self.logger.error(error_msg)
                    errors.append({"device": device.code, "error": str(e), "time": time.time()})

                    # 更新健康状态
                    device_health[device_id]["consecutive_failures"] += 1

                    # 检查超时
                    last_success = device_health[device_id]["last_success"]
                    if last_success and (time.time() - last_success) > connection_timeout:
                        device_health[device_id]["status"] = "timeout"
                        self.logger.warning(f"Device {device.code} timeout detected")

                        # 断开连接，触发重连
                        try:
                            protocol.disconnect()
                        except:
                            pass
                        device_protocols[device_id] = None
                    else:
                        device_health[device_id]["status"] = "error"

            # === 批量写入检查 ===
            batch_elapsed = time.time() - batch_start_time

            if batch_buffer and (len(batch_buffer) >= batch_size or batch_elapsed >= batch_timeout):
                try:
                    self._write_to_storage(batch_buffer)
                    self.logger.info(f"Wrote {len(batch_buffer)} points to storage")
                    batch_buffer = []
                    batch_start_time = time.time()
                except Exception as e:
                    self.logger.error(f"Failed to write to storage: {e}")
                    errors.append({"type": "storage", "error": str(e), "time": time.time()})

            # 更新Session元数据
            self.session.metadata = {
                "total_readings": total_readings,
                "last_cycle": time.time(),
                "errors": errors[-10:],  # 保留最近10个错误
                "device_health": device_health,
            }
            self.session.save(update_fields=["metadata", "updated_at"])

            # 控制采集频率
            cycle_elapsed = time.time() - cycle_start
            cycle_interval = self._get_cycle_interval()
            if cycle_elapsed < cycle_interval:
                time.sleep(cycle_interval - cycle_elapsed)

    finally:
        # === 阶段3: 清理所有连接 ===
        self.logger.info("Disconnecting all devices...")

        for device_id, protocol in device_protocols.items():
            if protocol:
                try:
                    protocol.disconnect()
                    self.logger.info(f"Disconnected from device {self.device_groups[device_id]['device'].code}")
                except Exception as e:
                    self.logger.warning(f"Error disconnecting: {e}")

        # 写入剩余缓冲区数据
        if batch_buffer:
            try:
                self._write_to_storage(batch_buffer)
                self.logger.info(f"Wrote final {len(batch_buffer)} points to storage")
            except Exception as e:
                self.logger.error(f"Failed to write final batch: {e}")

    return {
        "total_readings": total_readings,
        "errors": errors[-10:],
        "device_health": device_health,
    }
```

#### 2. 数据格式化 (backend/acquisition/services/acquisition_service.py:385-402)

```python
def _format_for_storage(
    self,
    readings: List[Dict[str, Any]],
    device: config_models.Device
) -> List[Dict[str, Any]]:
    """
    格式化数据为InfluxDB存储格式

    Args:
        readings: 协议读取的原始数据
        device: 设备对象

    Returns:
        格式化的数据点列表
    """
    formatted = []

    for reading in readings:
        # 获取测点详情
        point = self.task.points.filter(code=reading["code"]).first()
        if not point:
            continue

        # 使用服务器当前时间作为时间戳（纳秒精度）
        current_timestamp = int(time.time() * 1e9)

        # 构建数据点
        data_point = {
            "measurement": device.metadata.get("device_a_tag", device.code) if device.metadata else device.code,
            "tags": {
                "site": device.site.code,
                "device": device.code,
                "point": reading["code"],
                "quality": reading.get("quality", "good"),
            },
            "fields": {
                reading["code"]: reading["value"],
            },
            "time": current_timestamp,
        }

        # 添加模板信息（中文名称、单位等）
        if point.template:
            data_point["tags"]["cn_name"] = point.template.name
            data_point["tags"]["unit"] = point.template.unit or "nan"

        formatted.append(data_point)

    return formatted
```

#### 3. InfluxDB写入 (backend/storage/influxdb.py:95-186)

```python
def write(self, points: List[Dict[str, Any]]) -> bool:
    """
    批量写入数据点到InfluxDB

    Args:
        points: 数据点列表，每个点包含:
            - measurement: 测量名称
            - tags: 标签字典
            - fields: 字段字典
            - time: 时间戳（纳秒）

    Returns:
        True if successful
    """
    if not points:
        return True

    try:
        # 尝试HTTP API写入
        influx_points = []

        for point in points:
            influx_point = InfluxPoint(point["measurement"])

            # 添加标签
            for tag_key, tag_value in point.get("tags", {}).items():
                influx_point = influx_point.tag(tag_key, str(tag_value))

            # 添加字段
            for field_key, field_value in point.get("fields", {}).items():
                influx_point = influx_point.field(field_key, field_value)

            # 添加时间戳
            if "time" in point:
                influx_point = influx_point.time(point["time"], WritePrecision.NS)

            influx_points.append(influx_point)

        # 批量写入
        self.write_api.write(bucket=self.bucket, org=self.org, record=influx_points)
        return True

    except Exception as e:
        self.logger.error(f"Failed to write to InfluxDB: {e}")

        # 回退到Docker Exec方式
        try:
            self.logger.info("Attempting docker exec fallback...")
            return self._write_via_docker(points)
        except Exception as docker_error:
            self.logger.error(f"Docker fallback also failed: {docker_error}")
            raise WriteError(f"InfluxDB write failed: {e}")
```

---

## 前端展示流程

### 流程图

```
用户访问采集控制页面
    ↓
AcquisitionControlPage.tsx 加载
    ↓
并发请求多个API
    ├─ GET /api/config/tasks/ (获取任务列表)
    ├─ GET /api/acquisition/sessions/active/ (获取活动会话)
    └─ GET /api/config/devices/ (获取设备列表)
    ↓
渲染UI
    ├─ 状态分布卡片 (运行/停止/错误统计)
    ├─ 任务控制面板 (启动/停止按钮)
    └─ 设备健康状态徽章
    ↓
用户点击"查看历史数据"
    ↓
DataVisualizationPage.tsx
    ↓
GET /api/acquisition/sessions/point-history/?point_code=xxx&start_time=-1h
    ↓
绘制ECharts图表
```

### 关键代码快照

#### 1. 采集控制页面 (frontend/src/pages/AcquisitionControlPage.tsx:23-85)

```typescript
const AcquisitionControlPage: React.FC = () => {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [activeSessions, setActiveSessions] = useState<AcquisitionSession[]>([]);
  const [devices, setDevices] = useState<Device[]>([]);
  const [loading, setLoading] = useState(true);

  // 加载数据
  const fetchData = async () => {
    setLoading(true);
    try {
      // 并发请求
      const [tasksRes, sessionsRes, devicesRes] = await Promise.all([
        fetch('http://localhost:8000/api/config/tasks/'),
        fetch('http://localhost:8000/api/acquisition/sessions/active/'),
        fetch('http://localhost:8000/api/config/devices/'),
      ]);

      const tasksData = await tasksRes.json();
      const sessionsData = await sessionsRes.json();
      const devicesData = await devicesRes.json();

      setTasks(tasksData);
      setActiveSessions(sessionsData);
      setDevices(devicesData);
    } catch (error) {
      console.error('加载数据失败:', error);
    } finally {
      setLoading(false);
    }
  };

  // 首次加载和定时刷新
  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 5000); // 每5秒刷新
    return () => clearInterval(interval);
  }, []);

  return (
    <div className="acquisition-control-page">
      <h1>数据采集控制</h1>

      {/* 状态统计 */}
      <div className="stats-grid">
        <div className="stat-card">
          <div className="stat-label">任务总数</div>
          <div className="stat-value">{tasks.length}</div>
        </div>

        <div className="stat-card stat-distribution">
          <div className="stat-label">状态分布</div>
          <div className="stat-distribution-content">
            <div className="stat-item">
              <span className="stat-mini-label">运行:</span>
              <span className="stat-mini-value stat-running">
                {activeSessions.filter((s) => s.status === 'running').length}
              </span>
            </div>
            <div className="stat-item">
              <span className="stat-mini-label">停止:</span>
              <span className="stat-mini-value stat-stopped">
                {tasks.length - activeSessions.filter((s) => s.status === 'running').length}
              </span>
            </div>
            <div className="stat-item">
              <span className="stat-mini-label">错误:</span>
              <span className="stat-mini-value stat-error">
                {activeSessions.filter((s) => s.status === 'error').length}
              </span>
            </div>
          </div>
        </div>

        <div className="stat-card">
          <div className="stat-label">活动会话</div>
          <div className="stat-value">{activeSessions.length}</div>
        </div>

        <div className="stat-card">
          <div className="stat-label">设备总数</div>
          <div className="stat-value">{devices.length}</div>
        </div>
      </div>

      {/* 任务列表 */}
      <div className="tasks-grid">
        {tasks.map((task) => {
          const activeSession = activeSessions.find((s) => s.task === task.id);
          return (
            <TaskControlPanel
              key={task.id}
              task={task}
              activeSession={activeSession}
              onRefresh={fetchData}
            />
          );
        })}
      </div>
    </div>
  );
};
```

#### 2. 任务控制面板 (frontend/src/components/acquisition/TaskControlPanel.tsx:20-120)

```typescript
const TaskControlPanel: React.FC<Props> = ({ task, activeSession, onRefresh }) => {
  const [isLoading, setIsLoading] = useState(false);

  // 获取设备健康状态
  const getDeviceHealth = () => {
    if (!activeSession || !activeSession.metadata) return null;

    const deviceHealth = activeSession.metadata.device_health;
    if (!deviceHealth || typeof deviceHealth !== 'object') return null;

    const devices = Object.values(deviceHealth);
    return devices.length > 0 ? devices[0] : null;
  };

  const deviceHealth = getDeviceHealth();
  const isRunning = activeSession && activeSession.status === 'running';

  // 启动任务
  const handleStart = async () => {
    setIsLoading(true);
    try {
      const response = await fetch(
        'http://localhost:8000/api/acquisition/sessions/start-task/',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ task_id: task.id }),
        }
      );

      const data = await response.json();

      if (response.ok) {
        console.log('验证结果:', data.validation);
        onRefresh();
      } else {
        alert(data.detail || '启动失败');
      }
    } catch (error) {
      console.error('启动失败:', error);
    } finally {
      setIsLoading(false);
    }
  };

  // 停止任务
  const handleStop = async () => {
    if (!activeSession) return;

    setIsLoading(true);
    try {
      const response = await fetch(
        `http://localhost:8000/api/acquisition/sessions/${activeSession.id}/stop/`,
        { method: 'POST' }
      );

      if (response.ok) {
        onRefresh();
      }
    } catch (error) {
      console.error('停止失败:', error);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="task-control-panel">
      <div className="task-header">
        <div className="task-info">
          <div className="task-title-row">
            <h3>
              <span className={`status-indicator status-${isRunning ? 'running' : 'stopped'}`}>
                {isRunning ? '●' : '○'}
              </span>
              {task.name}
            </h3>

            <div className="task-badges">
              {/* 启用/停用徽章 */}
              <span className={`badge badge-${task.is_active ? 'enabled' : 'disabled'}`}>
                {task.is_active ? '启用' : '停用'}
              </span>

              {/* 设备健康徽章 */}
              {deviceHealth && (
                <span className={`badge badge-device-${deviceHealth.status}`}>
                  设备状态: {
                    deviceHealth.status === 'healthy' ? '正常' :
                    deviceHealth.status === 'error' ? '错误' :
                    deviceHealth.status === 'timeout' ? '超时' : '断开'
                  }
                </span>
              )}
            </div>
          </div>

          <p className="task-code">任务编码: {task.code}</p>
          <p className="task-description">{task.description}</p>
        </div>

        <div className="task-actions">
          {isRunning ? (
            <button
              className="btn btn-stop"
              onClick={handleStop}
              disabled={isLoading}
            >
              {isLoading ? '停止中...' : '停止'}
            </button>
          ) : (
            <button
              className="btn btn-start"
              onClick={handleStart}
              disabled={isLoading || !task.is_active}
            >
              {isLoading ? '启动中...' : '启动'}
            </button>
          )}
        </div>
      </div>

      {/* 会话信息 */}
      {activeSession && (
        <div className="session-info">
          <div className="info-item">
            <span className="label">运行时长:</span>
            <span className="value">
              {Math.floor(activeSession.duration_seconds / 60)} 分钟
            </span>
          </div>

          {activeSession.metadata?.total_readings && (
            <div className="info-item">
              <span className="label">采集点数:</span>
              <span className="value">{activeSession.metadata.total_readings}</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
};
```

#### 3. 历史数据查询 (backend/acquisition/views.py:360-450)

```python
@action(detail=False, methods=["get"], url_path="point-history")
def point_history(self, request):
    """
    查询测点历史数据

    Query Parameters:
        - point_code: 测点编码 (必填)
        - start_time: 开始时间 (RFC3339或相对时间如"-1h")
        - end_time: 结束时间 (默认now())
        - limit: 返回记录数 (默认100)

    Returns:
        {
            "point_code": "temperature1",
            "count": 100,
            "data": [
                {
                    "time": "2025-10-12T10:00:00Z",
                    "value": 25.5,
                    "quality": "good",
                    "device": "device-001",
                    "site": "factory-a"
                },
                ...
            ]
        }
    """
    point_code = request.query_params.get("point_code")
    if not point_code:
        return Response({"detail": "缺少 point_code 参数"}, status=400)

    start_time = request.query_params.get("start_time", "-1h")
    end_time = request.query_params.get("end_time", "now()")
    limit = int(request.query_params.get("limit", 100))

    try:
        # 查询InfluxDB
        from storage import StorageRegistry

        influxdb = StorageRegistry.create("influxdb", {
            "host": settings.INFLUXDB_HOST,
            "port": settings.INFLUXDB_PORT,
            "token": settings.INFLUXDB_TOKEN,
            "org": settings.INFLUXDB_ORG,
            "bucket": settings.INFLUXDB_BUCKET,
        })

        # 构建Flux查询
        flux_query = f'''
        from(bucket:"{settings.INFLUXDB_BUCKET}")
          |> range(start: {start_time}, stop: {end_time})
          |> filter(fn: (r) => r["point"] == "{point_code}")
          |> filter(fn: (r) => r["_field"] == "{point_code}")
          |> limit(n: {limit})
          |> sort(columns: ["_time"], desc: true)
        '''

        result = influxdb.query(flux_query)

        # 格式化结果
        data = []
        for record in result:
            data.append({
                "time": record.get("_time"),
                "value": record.get("_value"),
                "quality": record.get("quality"),
                "device": record.get("device"),
                "site": record.get("site"),
                "cn_name": record.get("cn_name"),
                "unit": record.get("unit"),
            })

        return Response({
            "point_code": point_code,
            "count": len(data),
            "data": data,
        })

    except Exception as e:
        self.logger.error(f"Failed to query point history: {e}", exc_info=True)
        return Response({"detail": f"查询失败: {str(e)}"}, status=500)
```

---

## 新协议开发指南

### 步骤概览

```
1. 创建协议类文件
2. 实现BaseProtocol接口
3. 注册协议
4. 配置设备使用新协议
5. 测试验证
```

### 详细步骤

#### 步骤1: 创建协议类文件

在 `backend/acquisition/protocols/` 目录下创建新文件，例如 `opcua.py`:

```python
"""
OPC UA协议实现

文件位置: backend/acquisition/protocols/opcua.py
"""
import logging
from typing import List, Dict, Any

from opcua import Client  # 需要安装: pip install opcua
from .base import BaseProtocol


class OPCUAProtocol(BaseProtocol):
    """
    OPC UA协议实现

    配置参数:
        - endpoint: OPC UA服务器端点 (如 "opc.tcp://192.168.1.100:4840")
        - username: 用户名 (可选)
        - password: 密码 (可选)
        - security_mode: 安全模式 (可选)
    """

    def __init__(self, config: Dict[str, Any]):
        """
        初始化OPC UA协议

        Args:
            config: 协议配置
                - endpoint: str, OPC UA服务器端点
                - username: str (可选)
                - password: str (可选)
        """
        super().__init__(config)

        self.endpoint = config.get("endpoint")
        if not self.endpoint:
            raise ValueError("OPC UA endpoint is required")

        self.username = config.get("username")
        self.password = config.get("password")

        self.client = None
        self.logger = logging.getLogger(__name__)

    def connect(self) -> None:
        """
        连接到OPC UA服务器

        Raises:
            ConnectionError: 连接失败时抛出
        """
        try:
            self.client = Client(self.endpoint)

            # 设置认证（如果提供）
            if self.username and self.password:
                self.client.set_user(self.username)
                self.client.set_password(self.password)

            # 连接
            self.client.connect()

            self.logger.info(f"Connected to OPC UA server {self.endpoint}")

        except Exception as e:
            self.logger.error(f"Failed to connect to OPC UA server: {e}")
            raise ConnectionError(f"OPC UA connection failed: {e}")

    def disconnect(self) -> None:
        """
        断开与OPC UA服务器的连接
        """
        if self.client:
            try:
                self.client.disconnect()
                self.logger.info(f"Disconnected from OPC UA server {self.endpoint}")
            except Exception as e:
                self.logger.warning(f"Error disconnecting from OPC UA server: {e}")
            finally:
                self.client = None

    def read_points(self, points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        批量读取OPC UA节点数据

        Args:
            points: 测点配置列表，每个测点包含:
                - code: str, 测点编码
                - address: str, OPC UA节点ID (如 "ns=2;s=MyDevice.Temperature")
                - type: str, 数据类型 (可选)

        Returns:
            读取结果列表，每个结果包含:
                - code: str, 测点编码
                - value: Any, 测点值
                - quality: str, 数据质量 ("good" or "bad")
                - timestamp: int, 时间戳 (纳秒)

        Raises:
            RuntimeError: 如果未连接或读取失败
        """
        if not self.client:
            raise RuntimeError("OPC UA client not connected")

        results = []

        for point in points:
            code = point.get("code")
            node_id = point.get("address")

            if not code or not node_id:
                self.logger.warning(f"Invalid point config: {point}")
                continue

            try:
                # 获取节点
                node = self.client.get_node(node_id)

                # 读取值
                data_value = node.get_data_value()

                # 提取值和质量
                value = data_value.Value.Value
                quality = "good" if data_value.StatusCode.is_good() else "bad"

                # 应用系数和精度（如果提供）
                coefficient = point.get("coefficient", 1.0)
                precision = point.get("precision", 2)

                if isinstance(value, (int, float)):
                    value = round(value * coefficient, precision)

                results.append({
                    "code": code,
                    "value": value,
                    "quality": quality,
                    "timestamp": data_value.SourceTimestamp.timestamp() * 1e9,  # 转为纳秒
                })

                self.logger.debug(f"Read point {code}: {value}")

            except Exception as e:
                self.logger.error(f"Failed to read point {code} ({node_id}): {e}")
                results.append({
                    "code": code,
                    "value": None,
                    "quality": "bad",
                    "timestamp": None,
                })

        return results

    def write_point(self, point: Dict[str, Any], value: Any) -> bool:
        """
        写入单个OPC UA节点数据

        Args:
            point: 测点配置
                - code: str, 测点编码
                - address: str, OPC UA节点ID
            value: 要写入的值

        Returns:
            True if successful, False otherwise
        """
        if not self.client:
            raise RuntimeError("OPC UA client not connected")

        node_id = point.get("address")
        code = point.get("code")

        try:
            node = self.client.get_node(node_id)
            node.set_value(value)

            self.logger.info(f"Wrote point {code}: {value}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to write point {code}: {e}")
            return False
```

#### 步骤2: 实现BaseProtocol接口

BaseProtocol接口定义（参考）:

```python
"""
协议基类接口

文件位置: backend/acquisition/protocols/base.py
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Any


class BaseProtocol(ABC):
    """
    协议基类，所有协议实现必须继承此类
    """

    def __init__(self, config: Dict[str, Any]):
        """
        初始化协议

        Args:
            config: 协议配置字典
        """
        self.config = config

    @abstractmethod
    def connect(self) -> None:
        """
        连接到设备

        实现此方法来建立与设备的连接。
        应该设置必要的连接参数并建立通信。

        Raises:
            ConnectionError: 连接失败时抛出
        """
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """
        断开与设备的连接

        实现此方法来安全地断开与设备的连接。
        应该清理资源并关闭连接。
        """
        pass

    @abstractmethod
    def read_points(self, points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        批量读取测点数据

        Args:
            points: 测点配置列表，每个测点包含:
                - code: str, 测点编码
                - address: str, 地址/节点ID
                - type: str, 数据类型
                - num: int, 数量（用于数组）
                - coefficient: float, 系数
                - precision: int, 精度

        Returns:
            读取结果列表，每个结果包含:
                - code: str, 测点编码
                - value: Any, 测点值
                - quality: str, 数据质量 ("good"/"bad"/"uncertain")
                - timestamp: int, 时间戳（纳秒，可选）

        Raises:
            RuntimeError: 读取失败时抛出
        """
        pass

    @abstractmethod
    def write_point(self, point: Dict[str, Any], value: Any) -> bool:
        """
        写入单个测点数据

        Args:
            point: 测点配置
            value: 要写入的值

        Returns:
            True if successful, False otherwise
        """
        pass
```

#### 步骤3: 注册协议

在 `backend/acquisition/protocols/__init__.py` 中注册新协议:

```python
"""
协议模块初始化

文件位置: backend/acquisition/protocols/__init__.py
"""
from .base import BaseProtocol
from .registry import ProtocolRegistry

# 导入协议实现
from .modbus import ModbusTCPProtocol
from .mqtt import MQTTProtocol
from .opcua import OPCUAProtocol  # 新增

# 注册协议
ProtocolRegistry.register("modbus", ModbusTCPProtocol)
ProtocolRegistry.register("modbus_tcp", ModbusTCPProtocol)
ProtocolRegistry.register("modbustcp", ModbusTCPProtocol)
ProtocolRegistry.register("mqtt", MQTTProtocol)

# 注册新协议
ProtocolRegistry.register("opcua", OPCUAProtocol)
ProtocolRegistry.register("opc_ua", OPCUAProtocol)
ProtocolRegistry.register("opc-ua", OPCUAProtocol)

__all__ = ["BaseProtocol", "ProtocolRegistry"]
```

#### 步骤4: 配置设备使用新协议

在Excel导入文件或通过API配置设备时，设置 `protocol` 字段为新协议名称：

**Excel示例**:
```
| device_a_tag | protocol | ip           | port | ...
|--------------|----------|--------------|------|
| opcua-dev-01 | opcua    | 192.168.1.100| 4840 | ...
```

**API示例**:
```python
# 创建OPC UA设备
device = Device.objects.create(
    site=site,
    code="opcua-dev-01",
    name="OPC UA Test Device",
    protocol="opcua",  # 使用新协议
    ip_address="192.168.1.100",
    port=4840,
    metadata={
        "endpoint": "opc.tcp://192.168.1.100:4840",
        "username": "admin",
        "password": "password",
    }
)
```

#### 步骤5: 测试验证

##### 5.1 单元测试

创建 `backend/tests/test_opcua_protocol.py`:

```python
"""
OPC UA协议单元测试
"""
import pytest
from acquisition.protocols import ProtocolRegistry


def test_opcua_protocol_creation():
    """测试OPC UA协议实例创建"""
    config = {
        "endpoint": "opc.tcp://localhost:4840",
        "username": "admin",
        "password": "admin",
    }

    protocol = ProtocolRegistry.create("opcua", config)
    assert protocol is not None
    assert protocol.endpoint == "opc.tcp://localhost:4840"


def test_opcua_read_points():
    """测试OPC UA数据读取"""
    config = {
        "endpoint": "opc.tcp://localhost:4840",
    }

    protocol = ProtocolRegistry.create("opcua", config)

    try:
        protocol.connect()

        points = [
            {"code": "temp1", "address": "ns=2;s=Temperature"},
            {"code": "press1", "address": "ns=2;s=Pressure"},
        ]

        results = protocol.read_points(points)

        assert len(results) == 2
        assert results[0]["code"] == "temp1"
        assert "value" in results[0]

    finally:
        protocol.disconnect()
```

##### 5.2 集成测试

使用Django管理命令测试:

```bash
cd backend

# 测试协议连接
python3 manage.py shell

>>> from acquisition.protocols import ProtocolRegistry
>>> config = {"endpoint": "opc.tcp://localhost:4840"}
>>> protocol = ProtocolRegistry.create("opcua", config)
>>> protocol.connect()
>>> points = [{"code": "test", "address": "ns=2;s=TestNode"}]
>>> results = protocol.read_points(points)
>>> print(results)
>>> protocol.disconnect()
```

##### 5.3 完整流程测试

1. 导入包含OPC UA设备的Excel配置
2. 在前端启动采集任务
3. 检查日志验证数据采集
4. 在数据可视化页面查看采集数据

```bash
# 查看采集日志
tail -f logs/celery_final.log | grep opcua

# 查询InfluxDB验证数据
docker exec influxdb influx query \
  'from(bucket: "iot-data")
   |> range(start: -5m)
   |> filter(fn: (r) => r["device"] == "opcua-dev-01")' \
  --org edge-iot --token my-super-secret-auth-token
```

### 协议开发最佳实践

#### 1. 错误处理

```python
def read_points(self, points):
    results = []

    for point in points:
        try:
            # 读取逻辑
            value = self._read_single_point(point)
            results.append({
                "code": point["code"],
                "value": value,
                "quality": "good",
            })
        except Exception as e:
            # 单个点失败不影响其他点
            self.logger.error(f"Failed to read {point['code']}: {e}")
            results.append({
                "code": point["code"],
                "value": None,
                "quality": "bad",
            })

    return results
```

#### 2. 连接管理

```python
def connect(self):
    # 检查是否已连接
    if self.is_connected():
        return

    try:
        # 建立连接
        self._establish_connection()

        # 验证连接
        self._verify_connection()

    except Exception as e:
        # 清理资源
        self._cleanup()
        raise ConnectionError(f"Connection failed: {e}")
```

#### 3. 日志记录

```python
import logging

class CustomProtocol(BaseProtocol):
    def __init__(self, config):
        super().__init__(config)
        self.logger = logging.getLogger(__name__)

    def connect(self):
        self.logger.info(f"Connecting to {self.config['host']}")
        # 连接逻辑
        self.logger.info("Connection established")

    def read_points(self, points):
        self.logger.debug(f"Reading {len(points)} points")
        # 读取逻辑
        self.logger.debug(f"Read {len(results)} values")
```

#### 4. 配置验证

```python
def __init__(self, config):
    super().__init__(config)

    # 验证必需参数
    required = ["host", "port"]
    for param in required:
        if param not in config:
            raise ValueError(f"Missing required parameter: {param}")

    # 验证参数类型
    if not isinstance(config["port"], int):
        raise TypeError("Port must be integer")

    # 设置默认值
    self.timeout = config.get("timeout", 30)
```

### 协议目录结构

```
backend/acquisition/protocols/
├── __init__.py           # 协议注册
├── base.py              # 基类接口
├── registry.py          # 协议注册表
├── modbus.py            # Modbus TCP协议
├── mqtt.py              # MQTT协议
├── opcua.py             # OPC UA协议 (新增)
├── siemens_s7.py        # Siemens S7协议 (新增)
└── profinet.py          # Profinet协议 (新增)
```

---

## 总结

### 系统数据流关键点

1. **Excel导入**: 异步处理 → ConfigVersion快照
2. **任务启动**: 同步验证(5秒) → Celery后台任务
3. **数据采集**: 持久连接 → 批量读取 → 批量写入
4. **前端展示**: 轮询API → 实时更新 → 历史查询

### 新协议开发核心

1. **继承BaseProtocol** - 实现4个核心方法
2. **注册协议** - 在ProtocolRegistry中注册
3. **测试验证** - 单元测试 + 集成测试
4. **错误处理** - 健壮的异常处理
5. **日志记录** - 详细的调试信息

通过这个指南，您可以完整理解系统的运作流程，并能够快速开发和集成新的工业协议！
