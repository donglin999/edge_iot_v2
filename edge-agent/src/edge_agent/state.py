"""Edge-side configuration cache.

Each ``apply_config`` frame from the center is persisted into the edge's
local SQLite (``edge_state.db``) so that:

1. The acquisition pipeline can read it via the existing Django ORM (the
   same models the center uses — we deliberately reuse them rather than
   maintain a second schema).
2. On edge-agent restart we can replay the latest snapshot from disk and
   resume the assigned tasks without waiting for the center to reconnect.

The cache stores the inbound ``apply_config`` *raw frame* in a single-row
``edge_state`` table alongside ``last_applied_version`` so the agent can
quickly tell whether an incoming frame is new without diffing every row.

This module is the single boundary between "protocol JSON" and "Django ORM
rows". Callers outside should treat it as opaque.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


_STATE_KV_DDL = """
CREATE TABLE IF NOT EXISTS edge_agent_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_KEY_LAST_FRAME = "last_apply_config_frame"
_KEY_LAST_VERSION = "last_applied_version"


@dataclass(frozen=True)
class CachedConfig:
    """Snapshot of the latest ``apply_config`` that was persisted."""

    version: int
    tasks: List[Dict[str, Any]]
    devices: List[Dict[str, Any]]
    points: List[Dict[str, Any]]
    # v0.4 (M4): threshold rules the edge evaluates locally. Optional on
    # the wire — a v0.3 center omits it and this stays an empty list.
    alarm_rules: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def task_ids(self) -> List[int]:
        return [int(t["id"]) for t in self.tasks]


class EdgeStateStore:
    """Owns the small KV table that survives across edge-agent restarts.

    Separate from the Django-managed tables (the ORM owns those). We
    keep this here in a tiny vanilla-sqlite layer because:

    * KV reads happen *before* Django bootstrap on a cold start (we want
      to know whether to re-import config without spinning up the ORM).
    * The Django ORM does not need a model for a 2-row metadata table.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_STATE_KV_DDL)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=30.0)

    def get_last_version(self) -> Optional[int]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT value FROM edge_agent_state WHERE key = ?",
                (_KEY_LAST_VERSION,),
            ).fetchone()
        if not row:
            return None
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return None

    def load_last_frame(self) -> Optional[Dict[str, Any]]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT value FROM edge_agent_state WHERE key = ?",
                (_KEY_LAST_FRAME,),
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            logger.warning("EdgeStateStore: cached frame is not valid JSON; discarding")
            return None

    def save_frame(self, frame: Dict[str, Any]) -> None:
        version = int(frame.get("version", 0))
        payload = json.dumps(frame, ensure_ascii=False)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO edge_agent_state(key, value) VALUES (?, ?)",
                    (_KEY_LAST_FRAME, payload),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO edge_agent_state(key, value) VALUES (?, ?)",
                    (_KEY_LAST_VERSION, str(version)),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise


def parse_apply_config(frame: Dict[str, Any]) -> CachedConfig:
    """Minimal validation on an inbound ``apply_config`` frame.

    Raises ``ValueError`` on a frame that is structurally unfit to apply.
    Field-level validation (e.g. point.address present) is left to the
    persistence step where missing data surfaces as an IntegrityError.
    """
    if not isinstance(frame, dict):
        raise ValueError("apply_config frame is not an object")
    try:
        version = int(frame["version"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("apply_config missing/invalid version") from exc
    tasks = frame.get("tasks") or []
    devices = frame.get("devices") or []
    points = frame.get("points") or []
    alarm_rules = frame.get("alarm_rules") or []
    if not isinstance(tasks, list) or not isinstance(devices, list) or not isinstance(points, list):
        raise ValueError("tasks/devices/points must be lists")
    if not isinstance(alarm_rules, list):
        raise ValueError("alarm_rules must be a list")
    return CachedConfig(
        version=version,
        tasks=list(tasks),
        devices=list(devices),
        points=list(points),
        alarm_rules=list(alarm_rules),
    )


# ---------------------------------------------------------------------------
# ORM persistence — requires `ensure_setup()` to have been called first.
# ---------------------------------------------------------------------------


_EDGE_SITE_CODE = "edge"
_EDGE_SITE_NAME = "edge-local"


def persist_to_orm(cached: CachedConfig) -> None:
    """Reconcile the local Django ORM tables against the snapshot.

    Strategy: idempotent ``update_or_create`` per row, then delete any rows
    that the snapshot did not include (tasks/points/devices the edge no
    longer owns). Wrapped in a single transaction so a partial failure
    leaves the previous state intact.
    """
    from django.db import transaction

    from acquisition.models import AlarmRule
    from configuration.models import (
        AcqTask,
        Device,
        Point,
        PointTemplate,
        Site,
        TaskPoint,
    )

    with transaction.atomic():
        site, _ = Site.objects.get_or_create(
            code=_EDGE_SITE_CODE, defaults={"name": _EDGE_SITE_NAME}
        )

        # --- devices -----------------------------------------------------
        keep_device_ids: List[int] = []
        for dev in cached.devices:
            obj, _created = Device.objects.update_or_create(
                pk=int(dev["id"]),
                defaults={
                    "site": site,
                    "code": str(dev["code"]),
                    "name": dev.get("name") or dev["code"],
                    "protocol": dev.get("protocol") or "",
                    "ip_address": dev.get("ip_address") or "",
                    "port": dev.get("port"),
                    "metadata": dev.get("metadata") or {},
                },
            )
            keep_device_ids.append(obj.pk)

        # --- points (templates first, in-row) ----------------------------
        keep_point_ids: List[int] = []
        for pt in cached.points:
            tpl_payload = pt.get("template") or None
            template = None
            if tpl_payload:
                template, _ = PointTemplate.objects.update_or_create(
                    name=tpl_payload.get("name") or str(pt["code"]),
                    english_name=tpl_payload.get("english_name") or str(pt["code"]),
                    defaults={
                        "unit": tpl_payload.get("unit") or "",
                        "data_type": tpl_payload.get("data_type") or "float",
                        "coefficient": tpl_payload.get("coefficient") or 1.0,
                        "precision": tpl_payload.get("precision") or 2,
                    },
                )
            obj, _ = Point.objects.update_or_create(
                pk=int(pt["id"]),
                defaults={
                    "device_id": int(pt["device_id"]),
                    "template": template,
                    "code": str(pt["code"]),
                    "address": str(pt.get("address") or ""),
                    "sample_rate_hz": pt.get("sample_rate_hz") or 1.0,
                    "extra": pt.get("extra") or {},
                },
            )
            keep_point_ids.append(obj.pk)

        # --- tasks + TaskPoint join --------------------------------------
        keep_task_ids: List[int] = []
        for task in cached.tasks:
            obj, _ = AcqTask.objects.update_or_create(
                pk=int(task["id"]),
                defaults={
                    "code": str(task["code"]),
                    "name": task.get("name") or str(task["code"]),
                    "sample_rate_hz": task.get("sample_rate_hz") or 1.0,
                    "is_active": bool(task.get("is_active", True)),
                    "edge_id": None,  # edge-local DB has no fleet rows; keep null
                },
            )
            keep_task_ids.append(obj.pk)

            wanted_point_ids = {int(pid) for pid in (task.get("point_ids") or [])}
            # Drop unwanted TaskPoint rows for this task.
            TaskPoint.objects.filter(task=obj).exclude(
                point_id__in=wanted_point_ids
            ).delete()
            # Insert any missing TaskPoint rows.
            existing = set(
                TaskPoint.objects.filter(task=obj).values_list("point_id", flat=True)
            )
            for pid in wanted_point_ids - existing:
                TaskPoint.objects.create(task=obj, point_id=pid)

        # --- alarm rules (M4) -------------------------------------------
        # The center mirrors every active threshold rule to the edge under
        # the same pk so ``alarm_event.rule_id`` is stable across both
        # sides. The local AlarmSink evaluates these in-process.
        keep_rule_ids: List[int] = []
        for rule in cached.alarm_rules:
            obj, _ = AlarmRule.objects.update_or_create(
                pk=int(rule["id"]),
                defaults={
                    "name": str(rule.get("name") or f"rule-{rule['id']}"),
                    "point_code": str(rule.get("point_code") or ""),
                    "device_code": str(rule.get("device_code") or ""),
                    "operator": str(rule.get("operator") or "gt"),
                    "threshold": rule.get("threshold"),
                    "threshold_high": rule.get("threshold_high"),
                    "severity": str(rule.get("severity") or "warning"),
                    "is_active": bool(rule.get("is_active", True)),
                    "description": str(rule.get("description") or ""),
                },
            )
            keep_rule_ids.append(obj.pk)

        # --- cull anything not in the snapshot --------------------------
        AcqTask.objects.exclude(pk__in=keep_task_ids).delete()
        # Drop rules the center no longer sends (deleted / deactivated).
        AlarmRule.objects.exclude(pk__in=keep_rule_ids).delete()
        # Points/devices may still be referenced by other (now-deleted)
        # task rows, but with the snapshot being authoritative we drop
        # those too. Devices keep their (site, code) keys stable, so
        # next snapshot can re-use the same primary keys.
        Point.objects.exclude(pk__in=keep_point_ids).delete()
        Device.objects.filter(site=site).exclude(pk__in=keep_device_ids).delete()


def apply_frame(state: EdgeStateStore, frame: Dict[str, Any]) -> CachedConfig:
    """Validate ``frame``, persist to ORM + KV, and return the cached snapshot.

    Idempotent: re-applying the same frame is a no-op for the ORM side
    (``update_or_create`` matches on PK) and overwrites the KV row.
    The caller is responsible for sending the ``config_applied`` frame
    back to the center based on whether this function raised.
    """
    cached = parse_apply_config(frame)
    persist_to_orm(cached)
    state.save_frame(frame)
    return cached
