"""Services for handling configuration import workflows."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pandas as pd
from django.db import transaction
from django.utils import timezone

from configuration import models

logger = logging.getLogger(__name__)


@dataclass
class ImportSummary:
    """结构化导入结果，用于前端展示校验信息。"""

    rows_parsed: int = 0
    created_points: int = 0
    updated_points: int = 0
    connection_count: int = 0
    device_tag_count: int = 0
    device_created: int = 0
    device_updated: int = 0
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    metadata: Dict[str, str] = field(default_factory=dict)

    @property
    def is_successful(self) -> bool:
        return not self.errors

    def to_dict(self) -> Dict[str, object]:
        return {
            "rows_parsed": self.rows_parsed,
            "created_points": self.created_points,
            "updated_points": self.updated_points,
            "connection_count": self.connection_count,
            "device_tag_count": self.device_tag_count,
            "device_created": self.device_created,
            "device_updated": self.device_updated,
            "warnings": self.warnings,
            "errors": self.errors,
            "metadata": self.metadata,
        }


class ExcelImportService:
    """读取 Excel 配置并生成校验摘要/落库。"""

    REQUIRED_COLUMNS = {"protocol_type", "source_ip", "source_port", "en_name"}

    def __init__(self, job: models.ImportJob, excel_path: Path) -> None:
        self.job = job
        self.excel_path = excel_path

    @staticmethod
    def _clean_value(value):
        return None if pd.isna(value) else value

    def load_dataframe(self) -> pd.DataFrame:
        try:
            return pd.read_excel(self.excel_path, sheet_name=0)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"Excel 文件不存在: {self.excel_path}") from exc

    def run_validation(self) -> ImportSummary:
        summary = ImportSummary()
        try:
            df = self.load_dataframe()
        except FileNotFoundError as exc:
            summary.errors.append(str(exc))
            logger.exception("Excel 文件读取失败: %s", exc)
            return summary

        summary.rows_parsed = len(df.index)
        missing = [column for column in self.REQUIRED_COLUMNS if column not in df.columns]
        if missing:
            summary.errors.append(f"缺少必要列: {', '.join(missing)}")
            return summary

        connections = self._collect_connections(df)
        device_tags = self._collect_device_tags(df)

        summary.connection_count = len(connections)
        summary.device_tag_count = len(device_tags)
        summary.created_points = len(df.index)
        summary.metadata["protocols"] = ",".join(sorted({proto for proto, *_ in connections}))
        if device_tags:
            summary.metadata["device_tags"] = ",".join(sorted(device_tags))

        if not connections:
            summary.warnings.append("未检测到有效的采集连接（协议/IP/端口）。")

        return summary

    @staticmethod
    def _collect_connections(df: pd.DataFrame) -> Set[Tuple[str, str, int]]:
        connections: Set[Tuple[str, str, int]] = set()
        for _, row in df.iterrows():
            protocol_raw = row.get("protocol_type")
            ip_raw = row.get("source_ip")
            port_raw = row.get("source_port")

            if pd.isna(protocol_raw) or pd.isna(ip_raw) or pd.isna(port_raw):
                continue

            protocol = str(protocol_raw).strip().lower()
            ip_address = str(ip_raw).strip()
            try:
                port = int(port_raw)
            except (TypeError, ValueError):
                try:
                    port = int(float(port_raw))
                except (TypeError, ValueError):
                    logger.warning("无法解析端口: %s", port_raw)
                    continue

            if protocol and ip_address and port >= 0:
                connections.add((protocol, ip_address, port))
        return connections

    @staticmethod
    def _collect_device_tags(df: pd.DataFrame) -> Set[str]:
        candidates: Set[str] = set()
        for column in ("device_name", "device_a_tag"):
            if column in df.columns:
                tags = {
                    str(value).strip()
                    for value in df[column].dropna().unique()
                    if str(value).strip()
                }
                candidates.update(tags)
        return candidates

    def persist_summary(self, summary: ImportSummary) -> models.ImportJob:
        existing = self.job.summary or {}
        merged_summary = existing.copy()
        merged_summary.update(summary.to_dict())
        summary.metadata.setdefault("file_path", existing.get("file_path", str(self.excel_path)))
        summary.metadata.setdefault("site_code", existing.get("site_code", "default"))
        merged_summary["metadata"] = summary.metadata
        self.job.status = models.ImportJob.STATUS_VALIDATED if summary.is_successful else models.ImportJob.STATUS_FAILED
        self.job.summary = merged_summary
        self.job.save(update_fields=["status", "summary", "updated_at"])
        return self.job

    def compute_diff(self, site_code: str = 'default') -> Dict[str, object]:
        df = self.load_dataframe()
        connection_entries = []
        connection_keys = []
        for protocol, ip, port in self._collect_connections(df):
            connection_entries.append({"protocol": protocol, "ip_address": ip, "port": port})
            connection_keys.append((protocol, ip, port))

        point_entries = []
        point_keys = []
        for _, row in df.iterrows():
            protocol = str(row['protocol_type']).strip().lower()
            ip = str(row['source_ip']).strip()
            port = int(row['source_port'])
            code = str(row.get('en_name')).strip()
            entry = {"protocol": protocol, "ip_address": ip, "port": port, "code": code}
            point_entries.append(entry)
            point_keys.append((protocol, ip, port, code))

        existing_conn_entries = []
        existing_point_entries = []
        site = models.Site.objects.filter(code=site_code).first()
        if site:
            for device in models.Device.objects.filter(site=site):
                key = (device.protocol, device.ip_address, device.port or 0)
                existing_conn_entries.append({"protocol": device.protocol, "ip_address": device.ip_address, "port": device.port or 0})
                for point in device.points.all():
                    existing_point_entries.append({"protocol": device.protocol, "ip_address": device.ip_address, "port": device.port or 0, "code": point.code})

        conn_existing_keys = {(c['protocol'], c['ip_address'], c['port']) for c in existing_conn_entries}
        point_existing_keys = {(p['protocol'], p['ip_address'], p['port'], p['code']) for p in existing_point_entries}

        connections = {
            'to_create': [c for c in connection_entries if (c['protocol'], c['ip_address'], c['port']) not in conn_existing_keys],
            'to_remove': [c for c in existing_conn_entries if (c['protocol'], c['ip_address'], c['port']) not in connection_keys],
            'existing': [c for c in connection_entries if (c['protocol'], c['ip_address'], c['port']) in conn_existing_keys],
        }
        points = {
            'to_create': [p for p in point_entries if (p['protocol'], p['ip_address'], p['port'], p['code']) not in point_existing_keys],
            'to_remove': [p for p in existing_point_entries if (p['protocol'], p['ip_address'], p['port'], p['code']) not in point_keys],
            'existing': [p for p in point_entries if (p['protocol'], p['ip_address'], p['port'], p['code']) in point_existing_keys],
        }

        return {
            'site_code': site_code,
            'connections': connections,
            'points': points,
        }

    @transaction.atomic
    def apply(self, site_code: str = "default", created_by: str = "", mode: str = "merge") -> Dict[str, object]:
        """
        应用配置到数据库。

        Args:
            site_code: 站点编码
            created_by: 创建者
            mode: 导入模式
                - "replace": 替换模式 - 删除站点下所有设备和任务后重新导入
                - "merge": 合并模式 - 更新已有记录，创建新记录（默认）
                - "append": 追加模式 - 仅创建新记录，不修改已有记录

        Returns:
            导入结果统计
        """
        df = self.load_dataframe()
        site, _ = models.Site.objects.get_or_create(
            code=site_code,
            defaults={"name": site_code, "description": "自动创建"},
        )

        # Replace mode: delete all existing data for this site
        if mode == "replace":
            # Get all task IDs associated with this site's devices
            task_ids = list(models.AcqTask.objects.filter(
                points__device__site=site
            ).distinct().values_list('id', flat=True))

            # Also find orphaned tasks (tasks with no points)
            orphaned_task_ids = list(models.AcqTask.objects.filter(
                points__isnull=True
            ).distinct().values_list('id', flat=True))

            # Combine both sets of task IDs
            all_task_ids = list(set(task_ids + orphaned_task_ids))

            deleted_tasks = len(all_task_ids)
            deleted_devices = models.Device.objects.filter(site=site).count()

            logger.info(f"Replace mode: deleting {deleted_tasks} tasks (including {len(orphaned_task_ids)} orphaned) and {deleted_devices} devices for site {site_code}")

            # Delete tasks first (to avoid orphaned tasks)
            if all_task_ids:
                models.AcqTask.objects.filter(id__in=all_task_ids).delete()

            # Then delete devices (cascade deletes points)
            models.Device.objects.filter(site=site).delete()

        device_cache: Dict[Tuple[str, str, int], models.Device] = {}
        created_devices = 0
        updated_devices = 0
        created_points = 0
        updated_points = 0
        skipped_devices = 0
        skipped_points = 0

        # Process devices
        for _, row in df.groupby(["protocol_type", "source_ip", "source_port"]).first().reset_index().iterrows():
            protocol = str(row["protocol_type"]).strip().lower()
            ip = str(row["source_ip"]).strip()
            port = int(row["source_port"])
            device_code = f"{protocol}-{ip}-{port}"
            device_name = str(row.get("device_name") or row.get("device_a_tag") or device_code)
            defaults = {
                "name": device_name,
                "code": device_code,
            }

            if mode == "append":
                # Append mode: only create new devices
                device, created = models.Device.objects.get_or_create(
                    site=site,
                    protocol=protocol,
                    ip_address=ip,
                    port=port,
                    defaults=defaults,
                )
                if created:
                    created_devices += 1
                else:
                    skipped_devices += 1
            else:
                # Merge or Replace mode: update existing or create new
                device, created = models.Device.objects.update_or_create(
                    site=site,
                    protocol=protocol,
                    ip_address=ip,
                    port=port,
                    defaults=defaults,
                )
                if created:
                    created_devices += 1
                else:
                    updated_devices += 1

            device_cache[(protocol, ip, port)] = device

        template_cache: Dict[str, models.PointTemplate] = {}
        point_records = []
        for _, row in df.iterrows():
            protocol = str(row["protocol_type"]).strip().lower()
            ip = str(row["source_ip"]).strip()
            port = int(row["source_port"])
            device = device_cache[(protocol, ip, port)]

            en_name = str(row.get("en_name")).strip()
            cn_name = str(row.get("cn_name") or en_name).strip()
            unit = str(row.get("unit") or "").strip()
            data_type = str(row.get("type") or "float").strip()
            coefficient_value = row.get("coefficient")
            coefficient = float(coefficient_value) if not pd.isna(coefficient_value) else 1
            precision_value = row.get("precision")
            precision = int(precision_value) if not pd.isna(precision_value) else 2
            template_key = f"{en_name}:{unit}:{data_type}:{coefficient}:{precision}"
            if template_key not in template_cache:
                template, _ = models.PointTemplate.objects.get_or_create(
                    name=cn_name or en_name,
                    english_name=en_name,
                    defaults={
                        "unit": unit,
                        "data_type": data_type,
                        "coefficient": coefficient,
                        "precision": precision,
                    },
                )
                template_cache[template_key] = template
            template = template_cache[template_key]

            extra = {
                "device_a_tag": self._clean_value(row.get("device_a_tag")),
                "device_name": self._clean_value(row.get("device_name")),
                "data_source": self._clean_value(row.get("data_source")),
                "input_range": [
                    self._clean_value(row.get("input_data_minimum")),
                    self._clean_value(row.get("input_data_maximum")),
                ],
                "output_range": [
                    self._clean_value(row.get("output_data_minimum")),
                    self._clean_value(row.get("output_data_maximum")),
                ],
                "num": self._clean_value(row.get("num")),
                "type": self._clean_value(row.get("type")),
            }

            point_defaults = {
                "channel": None,
                "template": template,
                "address": str(row.get("source_addr") or "").strip(),
                "description": cn_name,
                "sample_rate_hz": float(row.get("fs")) if not pd.isna(row.get("fs")) else 1.0,
                "extra": extra,
            }

            if mode == "append":
                # Append mode: only create new points
                point, created = models.Point.objects.get_or_create(
                    device=device,
                    code=en_name,
                    defaults=point_defaults,
                )
                if created:
                    created_points += 1
                else:
                    skipped_points += 1
            else:
                # Merge or Replace mode: update existing or create new
                point, created = models.Point.objects.update_or_create(
                    device=device,
                    code=en_name,
                    defaults=point_defaults,
                )
                if created:
                    created_points += 1
                else:
                    updated_points += 1

            point_records.append(point)

        task_version_ids: List[int] = []
        for device in device_cache.values():
            # Use device name as task identifier to maintain task continuity
            # even when device IP/port changes
            task_code = f"task-{device.name.replace(' ', '_')}" if device.name else f"task-{device.code}"

            # Use update_or_create to ensure task name/description are updated
            task, _ = models.AcqTask.objects.update_or_create(
                code=task_code,
                defaults={"name": device.name, "description": f"自动导入任务 {device.name}"},
            )
            device_points = list(device.points.all())
            task.points.set(device_points)
            latest = task.versions.order_by("-version").first()
            next_version = (latest.version if latest else 0) + 1
            payload = {
                "device": device.code,
                "points": [
                    {
                        "code": p.code,
                        "address": p.address,
                        "description": p.description,
                        "sample_rate_hz": float(p.sample_rate_hz),
                    }
                    for p in device_points
                ],
            }
            version = models.ConfigVersion.objects.create(
                task=task,
                version=next_version,
                summary=f"导入作业 {self.job.id} 自动生成",
                created_by=created_by,
                payload=payload,
            )
            task_version_ids.append(version.id)

        result = {
            "mode": mode,
            "device_created": created_devices,
            "device_updated": updated_devices,
            "device_skipped": skipped_devices,
            "point_created": created_points,
            "point_updated": updated_points,
            "point_skipped": skipped_points,
            "task_versions": task_version_ids,
        }

        self.job.status = models.ImportJob.STATUS_APPLIED
        self.job.related_version_id = task_version_ids[0] if task_version_ids else None
        summary = self.job.summary or {}
        summary["apply_result"] = result
        summary["applied_at"] = timezone.now().isoformat()
        summary["import_mode"] = mode
        self.job.summary = summary
        self.job.save(update_fields=["status", "related_version", "summary", "updated_at"])

        logger.info(f"Import completed in {mode} mode: {result}")
        return result


def process_excel(job: models.ImportJob, excel_path: Path, site_code: str | None = None) -> ImportSummary:
    """用于 Celery 或同步调用的快捷入口。"""

    service = ExcelImportService(job=job, excel_path=excel_path)
    summary = service.run_validation()
    if site_code:
        summary.metadata.setdefault("site_code", site_code)
    summary.metadata.setdefault("file_path", str(excel_path))
    service.persist_summary(summary)
    return summary
