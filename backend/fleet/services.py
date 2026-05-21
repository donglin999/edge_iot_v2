"""Helper services for the fleet app."""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from configuration.models import AcqTask, Device, Point

from .consumers import edge_group_name
from .models import (
    AssignmentDesiredState,
    EdgeAssignment,
    EdgeNode,
    EdgeStatus,
    OFFLINE_AFTER,
    next_config_version,
)
from .protocol import make_apply_config


def sweep_stale_edges(now=None) -> int:
    """Flip any edge whose last_seen is older than OFFLINE_AFTER to offline.

    Returns the number of rows updated. Cheap to call inline from the
    list endpoint — it's a single UPDATE bounded by an index on `status`.
    """
    now = now or timezone.now()
    cutoff = now - OFFLINE_AFTER
    return (
        EdgeNode.objects.filter(status=EdgeStatus.ONLINE)
        .filter(Q(last_seen__lt=cutoff) | Q(last_seen__isnull=True))
        .update(status=EdgeStatus.OFFLINE, updated_at=now)
    )


# ---------------------------------------------------------------------------
# Assignment / apply_config snapshot
# ---------------------------------------------------------------------------


def _serialize_task(task: AcqTask, point_ids: List[int]) -> Dict[str, Any]:
    return {
        "id": task.id,
        "code": task.code,
        "name": task.name,
        "sample_rate_hz": float(task.sample_rate_hz),
        "is_active": bool(task.is_active),
        "point_ids": sorted(point_ids),
    }


def _serialize_device(device: Device) -> Dict[str, Any]:
    return {
        "id": device.id,
        "code": device.code,
        "name": device.name,
        "protocol": device.protocol,
        "ip_address": device.ip_address or "",
        "port": device.port,
        "metadata": dict(device.metadata or {}),
    }


def _serialize_point(point: Point) -> Dict[str, Any]:
    tpl = point.template
    return {
        "id": point.id,
        "device_id": point.device_id,
        "code": point.code,
        "address": point.address,
        "sample_rate_hz": float(point.sample_rate_hz),
        "extra": dict(point.extra or {}),
        "template": (
            {
                "name": tpl.name,
                "english_name": tpl.english_name,
                "unit": tpl.unit,
                "data_type": tpl.data_type,
                "coefficient": float(tpl.coefficient),
                "precision": int(tpl.precision),
            }
            if tpl is not None
            else None
        ),
    }


def build_apply_config_payload(edge: EdgeNode, *, version: int) -> Dict[str, Any]:
    """Build the full ``apply_config`` frame payload for one edge.

    The payload includes every active task currently assigned to the edge
    (``AcqTask.edge_id == edge.id``), every device referenced by those
    tasks' points, and every point — joined transitively so the edge has
    everything it needs to run without a follow-up fetch.
    """
    tasks_qs = (
        AcqTask.objects.filter(edge=edge)
        .prefetch_related("points__device", "points__template")
        .order_by("code")
    )

    tasks_payload: List[Dict[str, Any]] = []
    device_map: Dict[int, Device] = {}
    point_map: Dict[int, Point] = {}

    for task in tasks_qs:
        task_point_ids: List[int] = []
        for point in task.points.all():
            point_map[point.id] = point
            if point.device_id not in device_map:
                device_map[point.device_id] = point.device
            task_point_ids.append(point.id)
        tasks_payload.append(_serialize_task(task, task_point_ids))

    devices_payload = [_serialize_device(d) for d in sorted(device_map.values(), key=lambda d: d.code)]
    points_payload = [_serialize_point(p) for p in sorted(point_map.values(), key=lambda p: p.id)]

    return make_apply_config(
        version=version,
        tasks=tasks_payload,
        devices=devices_payload,
        points=points_payload,
    )


def reconcile_assignments(edge: EdgeNode) -> Tuple[int, List[EdgeAssignment]]:
    """Bring ``EdgeAssignment`` rows into agreement with ``AcqTask.edge``.

    Returns ``(new_version, assignments)`` — ``new_version`` is the bumped
    per-edge ``config_version`` stamped on every assignment row touched by
    this reconcile pass, and ``assignments`` is the latest snapshot.

    The desired single-owner model is enforced by the unique_together on
    ``(edge, task)``. A task whose ``edge`` flips to another edge results
    in the old edge's assignment row being deleted on its next sync; we
    drop here too so the just-synced edge sees the correct state.
    """
    with transaction.atomic():
        new_version = next_config_version(edge)
        owned_task_ids = set(
            AcqTask.objects.filter(edge=edge).values_list("id", flat=True)
        )

        # Drop assignments for tasks the edge no longer owns.
        EdgeAssignment.objects.filter(edge=edge).exclude(task_id__in=owned_task_ids).delete()

        # Insert/refresh assignments for currently-owned tasks.
        for task_id in owned_task_ids:
            EdgeAssignment.objects.update_or_create(
                edge=edge,
                task_id=task_id,
                defaults={
                    "desired_state": AssignmentDesiredState.RUNNING,
                    "config_version": new_version,
                },
            )

        # Any remaining assignment rows (i.e. ones we didn't just touch
        # because they refer to a task that is no longer owned) have
        # already been deleted above. Stamp version on the surviving rows
        # so the snapshot version is consistent across all of them.
        EdgeAssignment.objects.filter(edge=edge).update(
            config_version=new_version,
            updated_at=timezone.now(),
        )

        assignments = list(
            EdgeAssignment.objects.filter(edge=edge)
            .select_related("task")
            .order_by("task__code")
        )

    return new_version, assignments


def dispatch_apply_config(edge: EdgeNode, frame: Dict[str, Any]) -> bool:
    """Push an ``apply_config`` frame to the edge's open WS session.

    Returns ``True`` if the channel-layer accepted the message; the frame
    delivery is still best-effort (the edge may not be connected). The
    edge converges on reconnect because the center re-pushes the latest
    snapshot.
    """
    layer = get_channel_layer()
    if layer is None:
        return False
    group = edge_group_name(edge.pk)
    try:
        async_to_sync(layer.group_send)(group, {"type": "fleet.send", "frame": frame})
    except Exception:  # noqa: BLE001
        return False
    return True


def sync_assignments(edge: EdgeNode) -> Dict[str, Any]:
    """Top-level helper: reconcile assignments and push ``apply_config``.

    Used by the ``POST /api/fleet/edges/{id}/assignments/sync`` view and
    callable in tests / shells. Returns a dict summary suitable as the
    HTTP response body.
    """
    version, assignments = reconcile_assignments(edge)
    frame = build_apply_config_payload(edge, version=version)
    delivered = dispatch_apply_config(edge, frame)
    return {
        "edge_id": edge.id,
        "edge_name": edge.name,
        "config_version": version,
        "assignment_count": len(assignments),
        "device_count": len(frame["devices"]),
        "point_count": len(frame["points"]),
        "delivered": delivered,
        "frame": frame,
    }
