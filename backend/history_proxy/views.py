"""Center-side history proxy view (M6 / XIU-83).

The center exposes a single read endpoint, ``GET /api/history/points``,
that the ``/data`` page Drawer calls instead of the legacy single-machine
``point-history`` route. The view:

1. Resolves each ``task_id`` to its owning ``EdgeNode`` via
   ``AcqTask.edge`` and short-circuits to a 503 with the edge name when
   the edge is offline (``EdgeNode.is_stale()``).
2. Groups requested ``point_ids`` by edge so a multi-task request fans
   out *one* HTTP call per edge instead of N parallel calls per task.
3. Calls the edge's ``GET /history/points`` (Bearer token taken from the
   one-shot edge token cached on the center — see ``EdgeNode.token_hash``
   note below) with a strict 10s timeout per edge.
4. Merges responses keyed by ``edge_id`` so the UI can render
   "数据来源: edge X".

Legacy single-host fallback: when ``AcqTask.edge`` is ``None`` we run the
old direct-Influx query in-process. That keeps single-machine
deployments working unchanged.

The edge token policy is the M6 trade-off worth calling out: the center
stores only the salted hash of each edge's token (see
``fleet.EdgeNode.token_hash``), but it needs a Bearer credential to call
the edge HTTP server. We resolve the plaintext token from a center-side
operator-supplied secret table (``settings.EDGE_HISTORY_PROXY_TOKENS``,
a ``{edge_name: token}`` mapping read from env at boot). That keeps the
"never persist the plaintext register token" invariant intact: ops can
mint a *separate* read-only token per edge for the history endpoint and
hand it to the center as a config secret. Until that table is populated
we accept ``settings.EDGE_HISTORY_PROXY_DEFAULT_TOKEN`` (single
shared-secret mode) which is the convention used by the smoke compose.
"""
from __future__ import annotations

import concurrent.futures
import logging
from typing import Any, Dict, Iterable, List, Optional

import requests
from django.conf import settings
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.request import Request
from rest_framework.response import Response

from configuration.models import AcqTask
from fleet.models import EdgeNode

logger = logging.getLogger(__name__)


DEFAULT_TIMEOUT_S = 10.0
DEFAULT_FAN_OUT_WORKERS = 8


# ---- token resolution -------------------------------------------------------


def _resolve_edge_token(edge: EdgeNode) -> Optional[str]:
    """Look up the Bearer credential the center should send to ``edge``.

    Resolution order:
    1. Per-edge override ``settings.EDGE_HISTORY_PROXY_TOKENS[edge.name]``
    2. Shared-secret ``settings.EDGE_HISTORY_PROXY_DEFAULT_TOKEN``
    3. ``None`` — the caller surfaces this as a 503 with a clear reason.
    """
    tokens: Dict[str, str] = getattr(settings, "EDGE_HISTORY_PROXY_TOKENS", {}) or {}
    token = tokens.get(edge.name)
    if token:
        return token
    default = getattr(settings, "EDGE_HISTORY_PROXY_DEFAULT_TOKEN", "") or ""
    return default or None


def _resolve_edge_url(edge: EdgeNode) -> Optional[str]:
    """Discover the edge's history HTTP base URL.

    Three sources, in priority order:
    1. ``EdgeNode.labels['history_url']`` — set by the edge in its
       register frame (M6 convention).
    2. ``settings.EDGE_HISTORY_PROXY_URLS[edge.name]`` — operator override
       for cases where the edge cannot advertise its own externally
       reachable URL (NAT, etc.).
    3. ``None`` — surfaced to the client as a 503.
    """
    labels = edge.labels or {}
    url = labels.get("history_url")
    if url:
        return str(url).rstrip("/")
    overrides: Dict[str, str] = getattr(settings, "EDGE_HISTORY_PROXY_URLS", {}) or {}
    fallback = overrides.get(edge.name)
    if fallback:
        return str(fallback).rstrip("/")
    return None


# ---- request shape ----------------------------------------------------------


def _parse_csv_param(value: str) -> List[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def _validate_int(value: Optional[str], *, default: int, name: str) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer; got {value!r}")


# ---- main view --------------------------------------------------------------


@extend_schema(
    summary="历史回查代理 (M6)",
    description=(
        "中心代理:按 ``task_id`` 解析目标 edge,合并相同 edge 的请求,"
        "并行 fan-out 到 edge 的 ``GET /history/points``。"
        "edge offline 时返回 503 并标注 edge_id。"
    ),
)
@api_view(["GET"])
def history_points(request: Request) -> Response:
    """``GET /api/history/points?task_id=&point_ids=&start=&end=&agg=&limit=``.

    Query parameters
    ----------------
    task_id
        Comma-separated AcqTask ids. Each task contributes its owning
        edge to the fan-out plan.
    point_ids
        Comma-separated point codes to query. The center forwards them
        as-is to every targeted edge.
    start, end
        Time range; same accepted shapes as the edge ``/history/points``
        endpoint (relative like ``-5m``, RFC3339, or ``now()``).
    agg
        ``raw`` (default), ``1s``, or ``10s``.
    limit
        Per-edge result cap; the edge enforces its own hard ceiling on
        top of this.

    The response merges per-edge payloads under ``sources[edge_id]`` and
    includes an aggregate ``data`` array for callers that don't care
    which edge served which sample.
    """
    params = request.query_params

    task_ids_raw = _parse_csv_param(params.get("task_id", ""))
    point_ids = _parse_csv_param(params.get("point_ids", ""))
    start = params.get("start") or "-5m"
    end = params.get("end") or "now()"
    agg = params.get("agg") or "raw"

    if not task_ids_raw:
        return Response(
            {"error": {"code": "missing_task_id", "message": "task_id is required"}},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not point_ids:
        return Response(
            {"error": {"code": "missing_point_ids", "message": "point_ids is required"}},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        task_ids = [int(t) for t in task_ids_raw]
    except ValueError:
        return Response(
            {"error": {"code": "bad_task_id", "message": "task_id must be integers"}},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        limit = _validate_int(params.get("limit"), default=5000, name="limit")
    except ValueError as exc:
        return Response(
            {"error": {"code": "bad_limit", "message": str(exc)}},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        timeout = float(params.get("timeout") or DEFAULT_TIMEOUT_S)
    except ValueError:
        timeout = DEFAULT_TIMEOUT_S
    timeout = max(0.5, min(timeout, 30.0))

    tasks = (
        AcqTask.objects.select_related("edge")
        .filter(id__in=task_ids)
    )
    by_id = {t.id: t for t in tasks}
    missing = [tid for tid in task_ids if tid not in by_id]
    if missing:
        return Response(
            {"error": {
                "code": "unknown_task",
                "message": f"task_id(s) not found: {sorted(missing)}",
            }},
            status=status.HTTP_404_NOT_FOUND,
        )

    # ------------------------------------------------------------------
    # Plan fan-out: group tasks by edge. Tasks with edge=None fall back
    # to the legacy center-Influx path in-process so existing
    # single-host deployments keep working unchanged.
    # ------------------------------------------------------------------
    edge_to_tasks: Dict[int, List[AcqTask]] = {}
    legacy_tasks: List[AcqTask] = []
    for tid in task_ids:
        t = by_id[tid]
        if t.edge_id is None:
            legacy_tasks.append(t)
        else:
            edge_to_tasks.setdefault(t.edge_id, []).append(t)

    sources: Dict[str, Dict[str, Any]] = {}
    errors: Dict[str, Dict[str, Any]] = {}
    merged_data: List[Dict[str, Any]] = []

    edge_ids = list(edge_to_tasks.keys())
    if edge_ids:
        edges = {e.id: e for e in EdgeNode.objects.filter(id__in=edge_ids)}
        # Submit one request per edge. Keep workers small — center fan-out
        # is bounded by edge count, not point count.
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(len(edges) or 1, DEFAULT_FAN_OUT_WORKERS)
        ) as pool:
            futures = {
                pool.submit(
                    _query_edge,
                    edge=edges[eid],
                    point_ids=point_ids,
                    start=start,
                    end=end,
                    agg=agg,
                    limit=limit,
                    timeout=timeout,
                ): eid
                for eid in edge_ids
                if eid in edges
            }
            for fut in concurrent.futures.as_completed(futures):
                eid = futures[fut]
                edge = edges[eid]
                try:
                    payload = fut.result()
                except _EdgeProxyError as exc:
                    errors[edge.name] = {
                        "edge_id": edge.name,
                        "status": exc.status_code,
                        "code": exc.code,
                        "message": exc.message,
                    }
                    continue
                sources[edge.name] = payload
                # Stamp each datum with its source edge so the UI can
                # render which row came from where.
                for row in payload.get("data") or []:
                    merged_data.append({**row, "edge_id": edge.name})

    # ------------------------------------------------------------------
    # Legacy single-host tasks: run the existing point_history Flux query
    # directly. Kept inline (and small) so the failure path of the proxy
    # cannot break single-host operation.
    # ------------------------------------------------------------------
    if legacy_tasks:
        try:
            legacy_payload = _query_center_legacy(
                point_ids=point_ids,
                start=start,
                end=end,
                limit=limit,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("history-proxy: legacy center query failed")
            errors["__center__"] = {
                "edge_id": None,
                "status": 500,
                "code": "center_query_failed",
                "message": str(exc),
            }
        else:
            sources["__center__"] = legacy_payload
            for row in legacy_payload.get("data") or []:
                merged_data.append({**row, "edge_id": None})

    # If we have errors *and* no successful sources, signal 503 so the UI
    # can render a single clear "edge X offline" message. Mixed
    # success/error returns 207-style 200 with both sources + errors so
    # the UI can selectively render what worked.
    overall_status = (
        status.HTTP_503_SERVICE_UNAVAILABLE
        if errors and not sources
        else status.HTTP_200_OK
    )

    return Response(
        {
            "task_ids": task_ids,
            "point_ids": point_ids,
            "start": start,
            "end": end,
            "agg": agg,
            "limit": limit,
            "count": len(merged_data),
            "sources": sources,
            "errors": errors,
            "data": merged_data,
            "queried_at": timezone.now().isoformat(),
        },
        status=overall_status,
    )


# ---- edge HTTP fan-out -----------------------------------------------------


class _EdgeProxyError(Exception):
    """Internal error surfaced as a per-edge entry in ``errors``."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _query_edge(
    *,
    edge: EdgeNode,
    point_ids: List[str],
    start: str,
    end: str,
    agg: str,
    limit: int,
    timeout: float,
) -> Dict[str, Any]:
    """Call one edge's ``/history/points``.

    Raises :class:`_EdgeProxyError` on any failure so the caller can
    map it into the per-edge ``errors`` slot of the response.
    """
    # M1: an edge that hasn't heartbeat'd within OFFLINE_AFTER is offline.
    # Don't waste the 10s timeout discovering it the hard way.
    if edge.is_stale():
        last_seen = edge.last_seen.isoformat() if edge.last_seen else None
        raise _EdgeProxyError(
            503,
            "edge_offline",
            f"edge {edge.name!r} is offline (last_seen={last_seen})",
        )

    url = _resolve_edge_url(edge)
    if not url:
        raise _EdgeProxyError(
            503,
            "edge_url_unknown",
            f"edge {edge.name!r} did not advertise history_url and no "
            "EDGE_HISTORY_PROXY_URLS override is configured",
        )
    token = _resolve_edge_token(edge)
    if not token:
        raise _EdgeProxyError(
            500,
            "edge_token_missing",
            f"no proxy token configured for edge {edge.name!r}; set "
            "EDGE_HISTORY_PROXY_DEFAULT_TOKEN or EDGE_HISTORY_PROXY_TOKENS",
        )

    query = {
        "point_ids": ",".join(point_ids),
        "start": start,
        "end": end,
        "agg": agg,
        "limit": str(limit),
    }
    headers = {"Authorization": f"Bearer {token}"}

    try:
        resp = requests.get(
            f"{url}/history/points",
            params=query,
            headers=headers,
            timeout=timeout,
        )
    except requests.Timeout as exc:
        raise _EdgeProxyError(
            504, "edge_timeout",
            f"edge {edge.name!r} timed out after {timeout:.1f}s",
        ) from exc
    except requests.RequestException as exc:
        raise _EdgeProxyError(
            502, "edge_unreachable",
            f"edge {edge.name!r} unreachable: {exc}",
        ) from exc

    if resp.status_code == 401:
        raise _EdgeProxyError(
            502, "edge_auth_rejected",
            f"edge {edge.name!r} rejected the proxy token",
        )
    if resp.status_code >= 400:
        message = resp.text[:200] if resp.text else f"HTTP {resp.status_code}"
        raise _EdgeProxyError(
            502 if resp.status_code >= 500 else resp.status_code,
            "edge_http_error",
            f"edge {edge.name!r} returned HTTP {resp.status_code}: {message}",
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise _EdgeProxyError(
            502, "edge_bad_json",
            f"edge {edge.name!r} returned non-JSON body",
        ) from exc


# ---- legacy fallback --------------------------------------------------------


def _query_center_legacy(
    *,
    point_ids: List[str],
    start: str,
    end: str,
    limit: int,
) -> Dict[str, Any]:
    """Direct Flux query against the center InfluxDB (single-host mode).

    Mirrors the older ``acquisition.point_history`` view; kept here so a
    single-host deployment keeps working when no ``AcqTask.edge`` is set.
    Per-point queries are inlined to keep the wire shape consistent with
    the edge proxy path.
    """
    from storage import StorageRegistry

    cfg = {
        "url": getattr(settings, "INFLUXDB_URL", None),
        "host": getattr(settings, "INFLUXDB_HOST", "localhost"),
        "port": getattr(settings, "INFLUXDB_PORT", 8086),
        "token": getattr(settings, "INFLUXDB_TOKEN", ""),
        "org": getattr(settings, "INFLUXDB_ORG", "default"),
        "bucket": getattr(settings, "INFLUXDB_BUCKET", "default"),
    }
    storage = StorageRegistry.create("influxdb", cfg)
    storage.connect()
    try:
        bucket = cfg["bucket"]
        safe_filters = " or ".join(
            f'r["_field"] == "{_escape_flux(pid)}"' for pid in point_ids
        )
        flux = (
            f'from(bucket:"{_escape_flux(bucket)}") '
            f'|> range(start: {_flux_range_arg(start)}, stop: {_flux_range_arg(end)}) '
            f'|> filter(fn: (r) => {safe_filters}) '
            f'|> sort(columns: ["_time"]) '
            f'|> limit(n: {int(limit)})'
        )
        records = storage.query(flux)
        data: List[Dict[str, Any]] = []
        for rec in records:
            ts = rec.get("_time")
            if ts is None:
                continue
            ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
            value = rec.get("_value")
            if not isinstance(value, (int, float, bool)) and value is not None:
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    pass
            data.append({
                "point_code": rec.get("_field") or rec.get("point_code"),
                "timestamp": ts_str,
                "value": value,
                "quality": rec.get("quality") or "good",
            })
        return {
            "edge_id": None,
            "point_ids": point_ids,
            "start": start,
            "end": end,
            "agg": "raw",
            "limit": limit,
            "count": len(data),
            "data": data,
            "truncated": len(data) >= int(limit),
            "elapsed_ms": 0,
        }
    finally:
        try:
            storage.disconnect()
        except Exception:  # noqa: BLE001
            pass


def _escape_flux(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _flux_range_arg(value: str) -> str:
    v = (value or "").strip()
    if not v:
        return "-5m"
    if v == "now()" or v.startswith("-") or v[:1].isdigit():
        return v
    return f'"{_escape_flux(v)}"'
