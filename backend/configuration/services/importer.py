"""Excel-driven configuration importer.

The importer is **schema-driven**: each row's ``protocol_type`` selects the
matching :class:`acquisition.protocols.BaseProtocol` subclass, whose
``DEVICE_FIELDS`` and ``POINT_FIELDS`` declarations drive validation,
coercion, and the resulting ``Device.metadata`` / ``Point.extra`` payloads.

This means adding a new protocol does NOT require touching this file — once
the protocol class declares its schema and ``IDENTITY_FIELDS`` the importer
picks it up automatically.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pandas as pd
from django.db import transaction
from django.utils import timezone

from acquisition.protocols import BaseProtocol, ProtocolRegistry
from configuration import models

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class RowError:
    row: int               # 1-based as a user sees in Excel (header is row 1)
    column: str = ""
    message: str = ""
    protocol: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"row": self.row, "column": self.column, "message": self.message, "protocol": self.protocol}


@dataclass
class ImportSummary:
    rows_parsed: int = 0
    created_points: int = 0
    updated_points: int = 0
    connection_count: int = 0
    device_tag_count: int = 0
    device_created: int = 0
    device_updated: int = 0
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    row_errors: List[RowError] = field(default_factory=list)
    metadata: Dict[str, str] = field(default_factory=dict)

    @property
    def is_successful(self) -> bool:
        return not self.errors and not self.row_errors

    def to_dict(self) -> Dict[str, Any]:
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
            "row_errors": [e.to_dict() for e in self.row_errors],
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _code_part(value: Any) -> str:
    """Render one identity value for a device code.

    pandas reads every integer cell as float64, so a port of 502 arrives as
    502.0 and used to produce ``modbus_tcp-192.168.1.100-502.0-1.0`` — while the
    same device added through the UI gets ``modbus_tcp-192.168.1.100-502-1``.
    Two codes for one device means the importer creates a duplicate instead of
    updating it. Integral floats therefore render as integers.
    """
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _device_code(protocol: str, identity: Tuple[Any, ...]) -> str:
    """Stable, human-recognisable Device.code, max 255 chars (model limit)."""
    base = f"{protocol}-" + "-".join(
        _code_part(v) for v in identity if v is not None and v != ""
    )
    if len(base) <= 255:
        # Replace characters that would confuse URLs / shells.
        return base.replace("/", "_").replace(":", "_").replace(" ", "_")
    digest = hashlib.sha1(base.encode()).hexdigest()[:12]
    short = base[:200].rstrip("-")
    return f"{short}-{digest}"


def _row_dict(row: pd.Series) -> Dict[str, Any]:
    """Convert a pandas row into a plain dict, dropping NaN."""
    out: Dict[str, Any] = {}
    for k, v in row.items():
        if pd.isna(v):
            continue
        out[str(k)] = v
    return out


# Persistence is chunked so SQLite's write lock is never held for the whole
# sheet at once (see ExcelImportService.apply). ~100 rows / devices per
# transaction keeps each lock hold short while still amortising commit cost.
_IMPORT_CHUNK_ROWS = 100
_IMPORT_CHUNK_DEVICES = 100


def _chunked(seq: List[Any], size: int):
    """Yield successive ``size``-length slices of ``seq``."""
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


# ---------------------------------------------------------------------------
# ExcelImportService
# ---------------------------------------------------------------------------


class ExcelImportService:
    """Read an Excel file, validate per-protocol, persist devices/points/tasks."""

    BASE_REQUIRED_COLUMNS = {"protocol_type", "code"}

    def __init__(self, job: models.ImportJob, excel_path: Path) -> None:
        self.job = job
        self.excel_path = excel_path

    # ---------- I/O ---------- #
    def load_dataframe(self) -> pd.DataFrame:
        """Load the first worksheet into a DataFrame via a streaming reader.

        ``openpyxl`` is opened with ``read_only=True``: the worksheet is never
        materialised into a full in-memory cell tree — rows are pulled lazily
        by ``iter_rows`` — so importer memory stays roughly proportional to a
        single row rather than the whole file.
        """
        from openpyxl import load_workbook
        from openpyxl.utils.exceptions import InvalidFileException

        if not Path(self.excel_path).exists():
            raise FileNotFoundError(f"Excel 文件不存在: {self.excel_path}")

        try:
            wb = load_workbook(self.excel_path, read_only=True, data_only=True)
        except InvalidFileException as exc:
            raise ValueError(f"无法解析 Excel 文件: {self.excel_path}") from exc

        try:
            ws = wb.worksheets[0]
            rows_iter = ws.iter_rows(values_only=True)
            try:
                header = next(rows_iter)
            except StopIteration:
                header = None
            if not header:
                return pd.DataFrame()
            columns = [str(h).strip() if h is not None else f"_col{i}"
                       for i, h in enumerate(header)]
            width = len(columns)
            records: List[Tuple[Any, ...]] = []
            for row in rows_iter:
                # openpyxl yields trailing empty rows and pads short rows with
                # None — drop wholly-empty rows and normalise the width.
                if row is None or all(v is None for v in row):
                    continue
                if len(row) < width:
                    row = tuple(row) + (None,) * (width - len(row))
                elif len(row) > width:
                    row = tuple(row[:width])
                records.append(row)
        finally:
            wb.close()

        df = pd.DataFrame.from_records(records, columns=columns)
        # Backward compat: older templates used `en_name` for the point code.
        if "code" not in df.columns and "en_name" in df.columns:
            df = df.rename(columns={"en_name": "code"})
        if "description" not in df.columns and "cn_name" in df.columns:
            df = df.rename(columns={"cn_name": "description"})
        return df

    # ---------- validation ---------- #
    def run_validation(self) -> ImportSummary:
        summary = ImportSummary()
        try:
            df = self.load_dataframe()
        except FileNotFoundError as exc:
            summary.errors.append(str(exc))
            return summary

        summary.rows_parsed = len(df.index)
        if summary.rows_parsed == 0:
            summary.errors.append("Excel 中没有数据行")
            return summary

        missing = [c for c in self.BASE_REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            summary.errors.append(f"缺少必要列: {', '.join(missing)}")
            return summary

        protocols_seen: set[str] = set()
        device_keys: set[Tuple[str, Tuple[Any, ...]]] = set()
        device_tags: set[str] = set()

        for idx, row in df.iterrows():
            row_num = idx + 2  # +2 because Excel header is row 1, idx is 0-based
            row_data = _row_dict(row)
            protocol = str(row_data.get("protocol_type", "")).strip().lower()
            if not protocol:
                summary.row_errors.append(RowError(row_num, "protocol_type", "缺少协议类型"))
                continue

            try:
                klass = ProtocolRegistry.get(protocol)
            except ValueError as exc:
                summary.row_errors.append(RowError(row_num, "protocol_type", str(exc), protocol=protocol))
                continue

            protocols_seen.add(klass.META.name)

            device_errors = klass.validate_device(row_data)
            for msg in device_errors:
                summary.row_errors.append(RowError(row_num, "", msg, protocol=protocol))

            point_errors = klass.validate_point(row_data)
            for msg in point_errors:
                summary.row_errors.append(RowError(row_num, "", msg, protocol=protocol))

            if not device_errors:
                identity = tuple(row_data.get(f) for f in klass.IDENTITY_FIELDS)
                device_keys.add((klass.META.name, identity))

            tag = row_data.get("device_name") or row_data.get("device_a_tag")
            if tag:
                device_tags.add(str(tag).strip())

        summary.connection_count = len(device_keys)
        summary.device_tag_count = len(device_tags)
        summary.created_points = summary.rows_parsed - len(summary.row_errors)
        summary.metadata["protocols"] = ",".join(sorted(protocols_seen))
        if device_tags:
            summary.metadata["device_tags"] = ",".join(sorted(device_tags))

        if not device_keys and not summary.row_errors:
            summary.warnings.append("未检测到有效的采集连接")
        return summary

    def persist_summary(self, summary: ImportSummary) -> models.ImportJob:
        existing = self.job.summary or {}
        merged = existing.copy()
        merged.update(summary.to_dict())
        summary.metadata.setdefault("file_path", existing.get("file_path", str(self.excel_path)))
        summary.metadata.setdefault("site_code", existing.get("site_code", "default"))
        merged["metadata"] = summary.metadata
        self.job.status = (
            models.ImportJob.STATUS_VALIDATED if summary.is_successful else models.ImportJob.STATUS_FAILED
        )
        self.job.summary = merged
        self.job.save(update_fields=["status", "summary", "updated_at"])
        return self.job

    # ---------- diff (preview before apply) ---------- #
    def compute_diff(self, site_code: str = "default") -> Dict[str, Any]:
        df = self.load_dataframe()

        connections: List[Dict[str, Any]] = []
        seen_keys: set[str] = set()
        point_entries: List[Dict[str, Any]] = []
        for idx, row in df.iterrows():
            row_data = _row_dict(row)
            protocol = str(row_data.get("protocol_type", "")).strip().lower()
            try:
                klass = ProtocolRegistry.get(protocol)
            except ValueError:
                continue
            identity = tuple(row_data.get(f) for f in klass.IDENTITY_FIELDS)
            code = _device_code(klass.META.name, identity)
            if code not in seen_keys:
                seen_keys.add(code)
                connections.append({
                    "protocol": klass.META.name,
                    "code": code,
                    "label": str(row_data.get("device_name") or row_data.get("device_a_tag") or code),
                })
            point_entries.append({
                "protocol": klass.META.name,
                "device_code": code,
                "code": str(row_data.get("code") or "").strip(),
            })

        existing_devices = list(models.Device.objects.filter(site__code=site_code))
        existing_codes = {d.code for d in existing_devices}
        existing_points = []
        for d in existing_devices:
            for p in d.points.all():
                existing_points.append({"device_code": d.code, "code": p.code, "protocol": d.protocol})
        existing_point_keys = {(p["device_code"], p["code"]) for p in existing_points}
        new_point_keys = {(p["device_code"], p["code"]) for p in point_entries}

        return {
            "site_code": site_code,
            "connections": {
                "to_create": [c for c in connections if c["code"] not in existing_codes],
                "existing": [c for c in connections if c["code"] in existing_codes],
                "to_remove": [
                    {"protocol": d.protocol, "code": d.code, "label": d.name}
                    for d in existing_devices if d.code not in {c["code"] for c in connections}
                ],
            },
            "points": {
                "to_create": [p for p in point_entries if (p["device_code"], p["code"]) not in existing_point_keys],
                "existing": [p for p in point_entries if (p["device_code"], p["code"]) in existing_point_keys],
                "to_remove": [p for p in existing_points if (p["device_code"], p["code"]) not in new_point_keys],
            },
        }

    # ---------- apply (write to DB) ---------- #
    def apply(self, site_code: str = "default", created_by: str = "", mode: str = "merge") -> Dict[str, Any]:
        """Validate, then persist devices/points/tasks to the database.

        The persistence phase is deliberately split into many *small*
        transactions (≈100 rows / devices each) instead of one sheet-wide
        ``transaction.atomic``. SQLite holds a database-level write lock for
        the entire life of a transaction, so a single long transaction
        starves every other writer (acquisition workers, the API) for the
        whole duration of the import. Chunking keeps each lock hold short.
        Validation runs first and fully outside any transaction.
        """
        # ---- phase 1: validate (no transaction held) ----
        summary = self.run_validation()
        if summary.row_errors:
            return {
                "status": "failed",
                "row_errors": [e.to_dict() for e in summary.row_errors],
                "errors": summary.errors,
            }

        rows = [_row_dict(row) for _, row in self.load_dataframe().iterrows()]

        # ---- phase 2: persist in bounded chunks ----
        with transaction.atomic():
            site, _ = models.Site.objects.get_or_create(
                code=site_code, defaults={"name": site_code, "description": "自动创建"},
            )
            if mode == "replace":
                # AcqTask cleanup is handled by the cascade signal on Device.
                models.Device.objects.filter(site=site).delete()

        device_cache: Dict[str, models.Device] = {}
        created_devices = updated_devices = skipped_devices = 0
        created_points = updated_points = skipped_points = 0

        # 1) Build/update devices, one row per (protocol, identity). We process
        #    the whole sheet first so the second pass can wire up points.
        device_meta_per_code: Dict[str, Tuple[type[BaseProtocol], Dict[str, Any]]] = {}
        for chunk in _chunked(rows, _IMPORT_CHUNK_ROWS):
            with transaction.atomic():
                for row_data in chunk:
                    protocol = str(row_data.get("protocol_type", "")).strip().lower()
                    klass = ProtocolRegistry.get(protocol)
                    identity = tuple(row_data.get(f) for f in klass.IDENTITY_FIELDS)
                    code = _device_code(klass.META.name, identity)
                    if code in device_cache:
                        continue

                    device_metadata = klass.coerce_device(row_data)
                    # Trim to declared fields so we don't leak point columns
                    # into device.metadata.
                    device_metadata = {f.name: device_metadata.get(f.name)
                                       for f in klass.DEVICE_FIELDS
                                       if f.name in device_metadata}
                    device_meta_per_code[code] = (klass, device_metadata)

                    ip_address = str(device_metadata.get("source_ip") or device_metadata.get("endpoint_url") or device_metadata.get("serial_port") or "")[:255]
                    port = device_metadata.get("source_port")
                    try:
                        port = int(port) if port is not None else None
                    except (TypeError, ValueError):
                        port = None

                    label = str(row_data.get("device_name") or row_data.get("device_a_tag") or code)
                    defaults = {
                        "site": site,
                        "name": label,
                        "protocol": klass.META.name,
                        "ip_address": ip_address,
                        "port": port,
                        "metadata": device_metadata,
                    }

                    if mode == "append":
                        device, created = models.Device.objects.get_or_create(code=code, defaults=defaults)
                        if created:
                            created_devices += 1
                        else:
                            skipped_devices += 1
                    else:
                        device, created = models.Device.objects.update_or_create(code=code, defaults=defaults)
                        if created:
                            created_devices += 1
                        else:
                            updated_devices += 1
                    device_cache[code] = device

        # 2) Persist points + per-row template
        for chunk in _chunked(rows, _IMPORT_CHUNK_ROWS):
            with transaction.atomic():
                for row_data in chunk:
                    protocol = str(row_data.get("protocol_type", "")).strip().lower()
                    klass = ProtocolRegistry.get(protocol)
                    identity = tuple(row_data.get(f) for f in klass.IDENTITY_FIELDS)
                    code = _device_code(klass.META.name, identity)
                    device = device_cache[code]

                    point_data = klass.coerce_point(row_data)
                    point_code = str(point_data.get("code") or "").strip()
                    if not point_code:
                        continue

                    data_type = str(point_data.get("data_type") or "float")
                    unit = str(point_data.get("unit") or "")
                    description = str(point_data.get("description") or point_code)
                    try:
                        coefficient = float(point_data.get("coefficient", 1.0))
                    except (TypeError, ValueError):
                        coefficient = 1.0

                    template, _ = models.PointTemplate.objects.get_or_create(
                        name=description or point_code,
                        english_name=point_code,
                        defaults={
                            "unit": unit,
                            "data_type": data_type,
                            "coefficient": coefficient,
                            "precision": 2,
                        },
                    )

                    extra = {f.name: point_data.get(f.name) for f in klass.POINT_FIELDS
                             if f.name in point_data and f.name != "code"}
                    extra["protocol"] = klass.META.name

                    point_defaults = {
                        "channel": None,
                        "template": template,
                        "address": str(point_data.get("address") or "").strip(),
                        "description": description,
                        "sample_rate_hz": 1.0,
                        "extra": extra,
                    }

                    if mode == "append":
                        _, created = models.Point.objects.get_or_create(
                            device=device, code=point_code, defaults=point_defaults,
                        )
                        if created:
                            created_points += 1
                        else:
                            skipped_points += 1
                    else:
                        _, created = models.Point.objects.update_or_create(
                            device=device, code=point_code, defaults=point_defaults,
                        )
                        if created:
                            created_points += 1
                        else:
                            updated_points += 1

        # 3) Auto-create / update tasks (1 task per device).
        task_version_ids: List[int] = []
        for device_chunk in _chunked(list(device_cache.values()), _IMPORT_CHUNK_DEVICES):
            with transaction.atomic():
                for device in device_chunk:
                    task_code = f"task-{device.name.replace(' ', '_')}" if device.name else f"task-{device.code}"

                    existing = list(models.AcqTask.objects.filter(points__device_id=device.id).distinct())
                    owned = [
                        t for t in existing
                        if not models.Point.objects.filter(tasks__id=t.id).exclude(device_id=device.id).exists()
                    ]
                    if owned:
                        owned.sort(key=lambda t: t.id)
                        task = owned[0]
                        task.code = task_code
                        task.name = device.name
                        task.description = f"自动导入任务 {device.name}"
                        task.save(update_fields=["code", "name", "description", "updated_at"])
                        for dup in owned[1:]:
                            dup.delete()
                    else:
                        task, _ = models.AcqTask.objects.update_or_create(
                            code=task_code,
                            defaults={"name": device.name, "description": f"自动导入任务 {device.name}"},
                        )

                    task.points.set(list(device.points.all()))
                    latest = task.versions.order_by("-version").first()
                    next_version = (latest.version if latest else 0) + 1
                    payload = {
                        "device": device.code,
                        "protocol": device.protocol,
                        "metadata": device.metadata,
                        "points": [{
                            "code": p.code,
                            "address": p.address,
                            "description": p.description,
                            "extra": p.extra,
                        } for p in device.points.all()],
                    }
                    version = models.ConfigVersion.objects.create(
                        task=task, version=next_version,
                        summary=f"导入作业 {self.job.id} 自动生成",
                        created_by=created_by, payload=payload,
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

        # ---- phase 3: record the outcome on the job (own short transaction) ----
        with transaction.atomic():
            self.job.status = models.ImportJob.STATUS_APPLIED
            self.job.related_version_id = task_version_ids[0] if task_version_ids else None
            sm = self.job.summary or {}
            sm["apply_result"] = result
            sm["applied_at"] = timezone.now().isoformat()
            sm["import_mode"] = mode
            self.job.summary = sm
            self.job.save(update_fields=["status", "related_version", "summary", "updated_at"])

        logger.info("Import applied (%s): %s", mode, result)
        return result


# Backward-compat helpers referenced by celery tasks / tests
def import_excel(job: models.ImportJob, excel_path: Path) -> ImportSummary:
    service = ExcelImportService(job=job, excel_path=excel_path)
    summary = service.run_validation()
    service.persist_summary(summary)
    return summary


def process_excel(
    job: models.ImportJob,
    excel_path: Path,
    site_code: str | None = None,
) -> ImportSummary:
    """Entrypoint used by ``configuration.tasks.process_excel_import``."""
    service = ExcelImportService(job=job, excel_path=excel_path)
    summary = service.run_validation()
    if site_code:
        summary.metadata["site_code"] = site_code
    service.persist_summary(summary)
    return summary
