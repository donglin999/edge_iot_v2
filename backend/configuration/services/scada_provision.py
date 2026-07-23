"""Bulk provisioning of SCADA devices + points + task under a gateway.

Replaces the old workflow where every 注塑机 was a spreadsheet row repeating
the whole MQTT connection block. The gateway already holds the shared
connection config, so a provision payload only carries what actually varies:
the ``device_name`` per machine and the ``code`` per point.

The whole operation is idempotent — re-posting the same payload updates in
place rather than duplicating — so the frontend can treat it as "save this
config" rather than "create these rows once".
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.db import transaction

from .. import models

#: Protocol name every provisioned device is created under.
SCADA_PROTOCOL = "scada"

#: Site code auto-created when the deployment has no Site at all yet.
DEFAULT_SITE_CODE = "default"


def _device_code(gateway: models.ScadaGateway, device_name: str) -> str:
    """Deterministic, unique ``Device.code`` for a gateway-backed device.

    Deterministic is what makes provisioning idempotent: the second call
    re-derives the same code and therefore finds the same row.
    """
    return f"scada-{gateway.code}-{device_name}"


def _resolve_site(site: Optional[models.Site]) -> models.Site:
    """Pick the Site to provision into.

    Explicit ``site`` wins; otherwise reuse the first existing Site so a
    single-site deployment (the common case) needs no site plumbing in the UI;
    otherwise bootstrap a ``default`` one.
    """
    if site is not None:
        return site
    existing = models.Site.objects.order_by("id").first()
    if existing is not None:
        return existing
    return models.Site.objects.create(code=DEFAULT_SITE_CODE, name="默认站点")


@transaction.atomic
def provision(gateway: models.ScadaGateway, data: Dict[str, Any]) -> Dict[str, Any]:
    """Create/update devices, points and the acquisition task under ``gateway``.

    Args:
        gateway: The gateway the devices hang off.
        data: Validated ``ScadaProvisionSerializer`` payload — ``site``
            (optional), ``devices`` (each with ``device_name``, ``name``,
            ``points``) and ``task`` (optional).

    Returns:
        The provision response body: gateway/site ids, the provisioned devices
        with their point ids, the task, and a count of what was newly created.
    """
    site = _resolve_site(data.get("site"))

    created_devices = 0
    created_points = 0
    device_payloads: List[Dict[str, Any]] = []
    # 一设备一任务:按设备分别收集测点,下面为每台设备各建一个任务。
    per_device_points: List[tuple] = []  # [(device, [points])]

    for device_data in data["devices"]:
        device_name = device_data["device_name"]
        code = _device_code(gateway, device_name)

        device, was_created = models.Device.objects.get_or_create(
            code=code,
            defaults={
                "site": site,
                "gateway": gateway,
                "name": device_data.get("name") or device_name,
                "protocol": SCADA_PROTOCOL,
                # Mirrored off the gateway purely so the existing device
                # list/搜索 UIs show something sensible — the values the
                # protocol actually uses are overlaid from the gateway at
                # acquisition time by ``build_device_config``.
                "ip_address": gateway.source_ip,
                "port": gateway.source_port,
                "metadata": {"scada_device_name": device_name},
            },
        )
        if was_created:
            created_devices += 1
        else:
            # Idempotent re-run: re-point an existing device at this gateway
            # and re-sync the mirrored fields, but preserve any device-specific
            # metadata overrides the operator added by hand.
            metadata = dict(device.metadata or {})
            metadata["scada_device_name"] = device_name
            device.site = site
            device.gateway = gateway
            device.name = device_data.get("name") or device_name
            device.protocol = SCADA_PROTOCOL
            device.ip_address = gateway.source_ip
            device.port = gateway.source_port
            device.metadata = metadata
            device.save()

        point_payloads: List[Dict[str, Any]] = []
        device_points: List[models.Point] = []
        for point_data in device_data.get("points") or []:
            point_code = point_data["code"]
            point, point_created = models.Point.objects.update_or_create(
                device=device,
                code=point_code,
                defaults={
                    "description": point_data.get("description") or point_code,
                    # For SCADA the point code *is* the address — it is the
                    # ``{code}`` segment of the MQTT topic.
                    "address": point_code,
                    # Mirrors the importer's convention: per-protocol POINT_FIELDS
                    # in ``extra``, tagged with the owning protocol.
                    "extra": {
                        "data_type": point_data.get("data_type") or "float",
                        "unit": point_data.get("unit") or "",
                        "protocol": SCADA_PROTOCOL,
                    },
                },
            )
            if point_created:
                created_points += 1
            device_points.append(point)
            point_payloads.append({"id": point.id, "code": point.code})

        per_device_points.append((device, device_points))
        device_payloads.append({
            "id": device.id,
            "device_name": device_name,
            "code": device.code,
            "points": point_payloads,
        })

    # 一设备一任务:任务模板里的 code/name 作前缀,每台设备派生出自己的任务
    # (task-{模板}-{设备名}),各自只绑本设备的测点。这样删设备能干净带走任务。
    task_payloads = _provision_tasks(data.get("task"), gateway, per_device_points)

    return {
        "gateway": gateway.id,
        "site": site.id,
        "devices": device_payloads,
        "tasks": task_payloads,
        # 兼容旧字段:单设备时给回第一个任务,多设备时为 None。
        "task": task_payloads[0] if len(task_payloads) == 1 else None,
        "created": {"devices": created_devices, "points": created_points},
    }


def _provision_tasks(
    task_data: Optional[Dict[str, Any]],
    gateway: models.ScadaGateway,
    per_device_points: List[tuple],
) -> List[Dict[str, Any]]:
    """为每台设备各建/更新一个采集任务(一设备一任务)。

    任务模板的 code 作前缀,加设备名派生出每台设备唯一的任务编码,避免多台设备
    抢同一个 task code。只有一台设备时任务编码就用模板 code 本身,保持简单。
    """
    if not task_data:
        return []

    base_code = task_data["code"]
    base_name = task_data.get("name") or base_code
    rate = task_data.get("sample_rate_hz") or Decimal("1.00")
    is_active = task_data.get("is_active", True)
    single = len(per_device_points) == 1

    payloads: List[Dict[str, Any]] = []
    for device, points in per_device_points:
        device_name = (device.metadata or {}).get("scada_device_name") or device.code
        code = base_code if single else f"{base_code}-{device_name}"
        name = base_name if single else f"{base_name} · {device_name}"
        task, _ = models.AcqTask.objects.update_or_create(
            code=code,
            defaults={"name": name, "sample_rate_hz": rate, "is_active": is_active},
        )
        # ``set``:全量期望状态,重复 provision 时被移除的测点会解绑。
        task.points.set(points)
        payloads.append({"id": task.id, "code": task.code, "device_id": device.id})
    return payloads
