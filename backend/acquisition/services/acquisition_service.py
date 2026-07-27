"""Core acquisition service for data collection orchestration.

The service is now a thin shell over :class:`AcquisitionPipeline`. Per-device
read loops, batching, and sink writes live in
``acquisition/services/pipeline.py`` and ``acquisition/services/sinks.py``;
this class only owns lifecycle (status transitions, startup validation, and
periodic health snapshots into ``session.metadata``).
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Any, Dict, List

from acquisition import models as acq_models
from acquisition.protocols import ProtocolRegistry  # noqa: F401  (kept for legacy imports)
from configuration import models as config_models

from .device_config import build_device_config
from .pipeline import AcquisitionPipeline, ReadWorker
from .sinks import WebSocketSink

logger = logging.getLogger(__name__)

# Per-device restart tracking. A device that crashes >= MAX_RESTARTS times
# within RESTART_WINDOW_S seconds is marked fatal and is not restarted again.
# These are soft limits — sized to absorb transient post-fork hiccups while
# still cutting off pathological crash loops.
MAX_RESTARTS_PER_WINDOW = 5
RESTART_WINDOW_S = 600.0  # 10 minutes

# Minimum interval between InfluxDB health writes (seconds)
HEALTH_WRITE_INTERVAL = 5.0
# Minimum interval between SQLite metadata updates (seconds)
SQLITE_METADATA_INTERVAL = 10.0
# Minimum interval between in-loop ``_update_session_health`` invocations.
# Bumped from 2s -> 5s so high-frequency (20 Hz) sessions don't pay the
# SQLite metadata-write cost too often. The InfluxDB and SQLite writes
# inside ``_update_session_health`` are themselves rate-limited by
# HEALTH_WRITE_INTERVAL / SQLITE_METADATA_INTERVAL above.
METADATA_WRITE_INTERVAL_S = 5.0


class AcquisitionService:
    """Orchestrates data acquisition lifecycle for a single session."""

    def __init__(
        self,
        task: config_models.AcqTask,
        session: acq_models.AcquisitionSession,
    ) -> None:
        self.task = task
        self.session = session
        self.logger = logging.getLogger(f"{__name__}.{task.code}")

        # Group points by device; this is the input to the pipeline.
        self.device_groups = self._group_points_by_device()

        # Health-update bookkeeping
        self._last_influxdb_health_write = 0.0
        self._last_sqlite_metadata_update = 0.0

        # Watchdog: per-device restart history. device_id -> (count, first_ts).
        # Keyed by device id so we can correlate across worker re-construction.
        self._restart_history: Dict[int, Dict[str, Any]] = {}
        # device_ids whose crash budget is exhausted; we stop trying to
        # restart these but the rest of the session keeps running.
        self._fatal_devices: set = set()

    # ------------------------------------------------------------------ setup
    def _group_points_by_device(self) -> Dict[int, Dict[str, Any]]:
        """Group points by device for batch reading."""
        groups: Dict[int, Dict[str, Any]] = defaultdict(
            lambda: {"device": None, "points": []}
        )

        for point in self.task.points.select_related("device", "template").all():
            device_id = point.device.id
            if groups[device_id]["device"] is None:
                groups[device_id]["device"] = point.device

            extras = dict(point.extra or {})
            point_config = {
                **extras,
                "code": point.code,
                "address": point.address,
                "coefficient": float(point.template.coefficient) if point.template else 1.0,
                "precision": int(point.template.precision) if point.template else 2,
            }
            if point.template:
                point_config.setdefault("cn_name", point.template.name)
                point_config.setdefault("unit", point.template.unit)
                # data_type 决定寄存器数与解码方式(float32 占 2 个寄存器,uint16 占 1)。
                # 只靠模板定型、没把 data_type 冗余进 point.extra 的测点(测点 CRUD 就是
                # 这种),如果这里不带上,读计划会回落 uint16 —— float32 被算成 1 个寄存器、
                # 当 uint16 解码,现场读出「半个值」,还被 coefficient/precision 掩盖。
                # 导入路径因为把 data_type 也塞进了 extra 而幸免,这里补上让三条路一致。
                point_config.setdefault("data_type", point.template.data_type)
            groups[device_id]["points"].append(point_config)

        return dict(groups)

    # ------------------------------------------------------------- single-shot
    def acquire_once(self) -> Dict[str, Any]:
        """Perform a single read pass for every device.

        Used by ``acquire_once`` celery task / debug commands. Re-uses the
        legacy ``read_points`` path for simplicity — the pipeline machinery
        is overkill for one-shot reads.
        """
        self.logger.info("Starting single acquisition for task %s", self.task.code)
        all_data: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []

        for device_id, group in self.device_groups.items():
            device = group["device"]
            points = group["points"]
            try:
                cfg = build_device_config(device)
                protocol = ProtocolRegistry.create(device.protocol, cfg)
                with protocol:
                    readings = protocol.read_points(points)
                    all_data.extend(readings)
            except Exception as exc:  # noqa: BLE001
                self.logger.error("Failed to read from device %s: %s", device.code, exc)
                errors.append({"device": device.code, "error": str(exc)})

        return {
            "status": "completed",
            "points_read": len(all_data),
            "errors": errors,
            "data": all_data,
        }

    # ----------------------------------------------------------- continuous
    def run_continuous(self) -> Dict[str, Any]:
        """Run the producer-consumer pipeline until the session is stopped."""
        self.logger.info("Starting pipeline acquisition for task %s", self.task.code)

        self.session.status = acq_models.AcquisitionSession.STATUS_RUNNING
        self.session.save(update_fields=["status", "updated_at"])

        sample_rate = float(self.task.sample_rate_hz)
        pipeline = AcquisitionPipeline(self.session, sample_rate_hz=sample_rate)
        pipeline.start(self.device_groups)

        # Block briefly so the first read completes before we report status.
        self._run_startup_validation(pipeline, timeout=2.0)

        last_meta_update = 0.0
        # Stash the pipeline so helpers (watchdog) can reach it.
        self.pipeline = pipeline
        try:
            while self._should_continue() and pipeline.is_alive():
                time.sleep(0.5)
                now = time.time()
                if now - last_meta_update >= METADATA_WRITE_INTERVAL_S:
                    self._supervise_workers(pipeline)
                    self._update_session_health(pipeline)
                    last_meta_update = now
        except KeyboardInterrupt:
            self.logger.info("Acquisition interrupted by user")
        finally:
            pipeline.stop(timeout=10.0)
            try:
                self._update_session_health(pipeline, force_sqlite=True)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("Final metadata update failed: %s", exc)

        return {
            "status": "completed",
            "device_health": pipeline.health,
        }

    # ----------------------------------------------------------- helpers
    def _should_continue(self) -> bool:
        """Re-read session status from the DB on every loop iteration."""
        try:
            self.session.refresh_from_db()
        except Exception:  # noqa: BLE001
            return False
        return self.session.status == acq_models.AcquisitionSession.STATUS_RUNNING

    def _run_startup_validation(self, pipeline: AcquisitionPipeline, timeout: float) -> None:
        """Wait briefly for the workers to take their first reading.

        Records a snapshot of per-device health into
        ``session.metadata.startup_validation`` so the API can surface a
        first-cycle status without polling InfluxDB.
        """
        deadline = time.time() + max(0.1, float(timeout))
        # Poll until either every device has a successful read or we time out.
        while time.time() < deadline:
            if self.device_groups and all(
                pipeline.health.get(d_id, {}).get("last_success")
                for d_id in self.device_groups
            ):
                break
            time.sleep(0.1)

        snapshot: Dict[str, Any] = {
            "checked_at": time.time(),
            "devices": {},
        }
        all_healthy = True
        for device_id, group in self.device_groups.items():
            device = group["device"]
            health = pipeline.health.get(device_id, {})
            status = health.get("status", "init")
            snapshot["devices"][device.code] = {
                "status": status,
                "consecutive_failures": health.get("consecutive_failures", 0),
                "last_success": health.get("last_success"),
            }
            if status != "healthy":
                all_healthy = False
        snapshot["all_healthy"] = all_healthy

        try:
            self.session.refresh_from_db(fields=["metadata"])
            meta = self.session.metadata or {}
            meta["startup_validation"] = snapshot
            self.session.metadata = meta
            self.session.save(update_fields=["metadata", "updated_at"])
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Failed to persist startup_validation: %s", exc)

    # ----------------------------------------------------------- watchdog
    def _supervise_workers(self, pipeline: AcquisitionPipeline) -> None:
        """Detect dead ReadWorker threads and restart them in place.

        Bounded by ``MAX_RESTARTS_PER_WINDOW`` per device per
        ``RESTART_WINDOW_S`` window — a device that crashes too often is
        considered fatal and is left dead so the rest of the session can
        continue without an infinite restart loop.

        Emits a ``worker_died`` connection_event through the WebSocketSink
        on each restart so the operator UI can render a lifecycle entry
        (we emit from the supervisor's vantage point because the worker
        thread is, by definition, no longer alive to emit it itself).
        """
        dead = pipeline.dead_workers()
        if not dead:
            return

        ws_sink = next((s for s in pipeline.sinks if isinstance(s, WebSocketSink)), None)
        now = time.time()

        for worker in dead:
            device = worker.device
            if device.id in self._fatal_devices:
                continue

            # --- update restart-window history --------------------------
            history = self._restart_history.get(device.id)
            if history is None or (now - history["first_ts"]) > RESTART_WINDOW_S:
                history = {"count": 0, "first_ts": now}
                self._restart_history[device.id] = history
            history["count"] += 1

            self.logger.warning(
                "ReadWorker[%s] died unexpectedly, restarting (attempt %d/%d in %.0fs window)",
                device.code,
                history["count"],
                MAX_RESTARTS_PER_WINDOW,
                RESTART_WINDOW_S,
            )

            # --- broadcast worker_died event to the operator UI ---------
            if ws_sink is not None:
                try:
                    ws_sink.consume_event({
                        "event": "worker_died",
                        "device_code": device.code,
                        "device_id": device.id,
                        "restart_count": history["count"],
                        "max_restarts": MAX_RESTARTS_PER_WINDOW,
                    })
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning(
                        "WebSocketSink emit worker_died failed for %s: %s",
                        device.code, exc,
                    )

            # --- enforce restart cap ------------------------------------
            if history["count"] > MAX_RESTARTS_PER_WINDOW:
                self.logger.error(
                    "ReadWorker[%s] exceeded restart budget (%d in %.0fs); "
                    "marking device fatal — no further restarts",
                    device.code, history["count"], RESTART_WINDOW_S,
                )
                self._fatal_devices.add(device.id)
                if ws_sink is not None:
                    try:
                        ws_sink.consume_event({
                            "event": "worker_fatal",
                            "device_code": device.code,
                            "device_id": device.id,
                            "restart_count": history["count"],
                        })
                    except Exception:  # noqa: BLE001
                        pass
                continue

            # --- construct a fresh worker with the same config ----------
            try:
                new_worker = ReadWorker(
                    device=worker.device,
                    points=worker.points,
                    sinks=worker.sinks,
                    sample_rate_hz=1.0 / worker.cycle_interval if worker.cycle_interval else pipeline.sample_rate_hz,
                    shutdown_event=pipeline.shutdown_event,
                    health_dict=pipeline.health,
                    max_reconnect=worker.max_reconnect,
                    connection_timeout=worker.connection_timeout,
                    reconnect_backoff=worker.reconnect_backoff,
                )
                pipeline.replace_worker(old=worker, new=new_worker)
            except Exception as exc:  # noqa: BLE001
                self.logger.error(
                    "Failed to construct replacement worker for %s: %s",
                    device.code, exc,
                )

    # ----------------------------------------------------------- health writes
    def _update_session_health(
        self,
        pipeline: AcquisitionPipeline,
        *,
        force_sqlite: bool = False,
    ) -> None:
        """Push health into InfluxDB (frequent) and SQLite (every ~10 s)."""
        now = time.time()
        device_health = pipeline.health

        if (now - self._last_influxdb_health_write) >= HEALTH_WRITE_INTERVAL and pipeline.influx_sink:
            health_points = self._format_health_data(device_health, now)
            try:
                pipeline.influx_sink.write_health_points(health_points)
                self._last_influxdb_health_write = now
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("Failed to write health to InfluxDB: %s", exc)

        if force_sqlite or (now - self._last_sqlite_metadata_update) >= SQLITE_METADATA_INTERVAL:
            self._update_sqlite_metadata(device_health, now)
            self._last_sqlite_metadata_update = now

    def _format_health_data(
        self,
        device_health: Dict[int, Dict[str, Any]],
        timestamp: float,
    ) -> List[Dict[str, Any]]:
        """Build per-device + aggregate health points for InfluxDB."""
        health_points: List[Dict[str, Any]] = []

        for device_id, health in device_health.items():
            device = self.device_groups.get(device_id, {}).get("device")
            if device is None:
                continue
            health_points.append({
                "measurement": "session_health",
                "tags": {
                    "session_id": str(self.session.id),
                    "task_code": self.task.code,
                    "device_code": device.code,
                    "device_ip": device.ip_address or "unknown",
                    "status": health.get("status", "unknown"),
                },
                "fields": {
                    "consecutive_failures": health.get("consecutive_failures", 0),
                    "last_success_ts": health.get("last_success") or 0,
                    "dropped_messages": health.get("dropped_messages", 0),
                    "session_status": self.session.status,
                },
                "time": int(timestamp * 1e9),
            })

        total_failures = sum(h.get("consecutive_failures", 0) for h in device_health.values())
        healthy_devices = sum(1 for h in device_health.values() if h.get("status") == "healthy")
        health_points.append({
            "measurement": "session_health",
            "tags": {
                "session_id": str(self.session.id),
                "task_code": self.task.code,
                "device_code": "_aggregate",
                "device_ip": "N/A",
                "status": "aggregate",
            },
            "fields": {
                "total_devices": len(device_health),
                "healthy_devices": healthy_devices,
                "total_consecutive_failures": total_failures,
                "session_status": self.session.status,
            },
            "time": int(timestamp * 1e9),
        })
        return health_points

    def _update_sqlite_metadata(
        self,
        device_health: Dict[int, Dict[str, Any]],
        timestamp: float,
    ) -> None:
        """Persist a health summary into ``session.metadata`` (SQLite)."""
        try:
            health_summary: Dict[str, Any] = {}
            for device_id, health in device_health.items():
                device = self.device_groups.get(device_id, {}).get("device")
                if device is None:
                    continue
                health_summary[device.code] = {
                    "status": health.get("status", "unknown"),
                    "consecutive_failures": health.get("consecutive_failures", 0),
                    "last_success": health.get("last_success"),
                    # 队列溢出累计丢弃(推模式协议;拉模式恒 0/缺省)。
                    "dropped_messages": health.get("dropped_messages", 0),
                }

            self.session.refresh_from_db(fields=["metadata"])
            meta = self.session.metadata or {}

            # 真实入库速率 —— 在覆盖旧值之前,用「累计写入量」和上一次写元数据的
            # 时间戳算出这段窗口内的平均点/秒。total_points_read 来自
            # InfluxDBSink.total_written,只在**写成功**时递增,所以这是真正落到
            # InfluxDB 的频率,而不是采集尝试的频率:InfluxDB 写不进去时它就是 0,
            # 这恰恰是要如实告诉操作员的。
            pipeline = getattr(self, "pipeline", None)
            new_total = pipeline.total_points_read if pipeline is not None else \
                int(meta.get("total_points_read", 0))
            prev_total = int(meta.get("total_points_read", 0))
            prev_ts = meta.get("last_health_update")
            if isinstance(prev_ts, (int, float)) and timestamp > prev_ts:
                dt = timestamp - prev_ts
                delta = max(new_total - prev_total, 0)
                meta["ingest_points_per_sec"] = round(delta / dt, 2)
                meta["ingest_measured_at"] = timestamp

            # 目标入库速率 —— 给实际速率一个参照,让人一眼看出达没达标。
            #
            # 口径必须和 pipeline 的**真实**行为一致:所有 ReadWorker 用同一个
            # task.sample_rate_hz,每个采集周期把全部测点各读一遍。所以
            #   目标点/秒 = 采样频率 × 测点总数
            # 注意:Point.sample_rate_hz / TaskPoint.overrides 目前在运行时被忽略,
            # 不参与目标计算(不能按「每测点频率求和/取最高」,那和实际不符)。
            # 测点数用 device_groups(与真正在跑的 worker 同源),不用启动校验快照。
            point_count = sum(len(g.get("points", [])) for g in self.device_groups.values())
            try:
                sample_rate = float(self.task.sample_rate_hz)
            except (TypeError, ValueError):
                sample_rate = 0.0
            meta["ingest_target_points_per_sec"] = round(sample_rate * point_count, 2)
            meta["ingest_target_basis"] = {
                "sample_rate_hz": sample_rate,
                "point_count": point_count,
            }

            meta["device_health"] = health_summary
            meta["last_health_update"] = timestamp
            # Pick the most recent successful read across devices as a
            # rough "last_read_time" — used by the recovery heartbeat.
            last_reads = [h.get("last_success") for h in device_health.values() if h.get("last_success")]
            if last_reads:
                meta["last_read_time"] = max(last_reads)
            # Surface cumulative successful-write count to the API. Pulled
            # straight from the pipeline (= InfluxDBSink.total_written).
            meta["total_points_read"] = new_total
            self.session.metadata = meta
            self.session.save(update_fields=["metadata", "updated_at"])
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Failed to update SQLite metadata: %s", exc)
