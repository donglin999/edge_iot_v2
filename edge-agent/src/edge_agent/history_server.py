"""Read-only HTTP history endpoint exposed by the edge (M6 / XIU-83).

In the distributed architecture the *only* place where raw acquisition
samples land is the **edge-side InfluxDB**. The single-machine ``/data``
page used to query the center's InfluxDB directly; under fleet mode the
center has no samples to serve. M6 closes that gap with a thin read-only
HTTP server inside the edge-agent process that proxies Flux queries
against the local InfluxDB on demand.

The contract is intentionally tiny:

* ``GET /history/points`` — issued by the center proxy (never by the
  browser). Authenticates with the same ``EDGE_TOKEN`` the WS register
  flow uses, via an ``Authorization: Bearer …`` header.
* ``GET /healthz`` — unauthenticated liveness probe.

Everything else returns ``404``. The server intentionally does **not**
re-implement Flux: it builds a query string and hands it to the existing
:class:`storage.influxdb.InfluxDBStorage` so the query path lives in one
place. We cap the result set at ``HISTORY_MAX_POINTS`` (default 50 000)
to keep a misbehaving caller from OOM-ing the agent.

The server is started as a background ``asyncio`` task in
:meth:`edge_agent.agent.EdgeAgent.run`, sibling to the WS connect loop —
it stays up across center reconnects so a transient WS drop does not
take history queries down with it.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional

from aiohttp import web

from .config import EdgeConfig

logger = logging.getLogger("edge_agent.history")


# Allowed aggregation windows. Keep this list small on purpose — exposing
# arbitrary durations would make the cap-on-result-count brittle.
AGG_RAW = "raw"
AGG_1S = "1s"
AGG_10S = "10s"
_ALLOWED_AGG = {AGG_RAW, AGG_1S, AGG_10S}


@dataclass(frozen=True)
class HistoryQuery:
    """Parsed + validated request parameters."""

    point_ids: List[str]
    start: str
    end: str
    agg: str
    limit: int


class HistoryError(Exception):
    """Validation / query failure surfaced as an HTTP error response."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _escape_flux_string(value: str) -> str:
    """Escape a string for embedding inside a Flux double-quoted literal.

    We accept point ids from the caller and embed them in a generated Flux
    ``filter()`` predicate; backslash and double-quote are the only chars
    that can break out of the literal.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _flux_range_arg(value: str) -> str:
    """Render an inbound start/end token into a Flux ``range()`` argument.

    Mirrors the rule already used by the center's
    ``acquisition.point_history`` view: relative durations (``-5m``,
    ``-1h``) and digit-leading literals (``2024-01-01T00:00:00Z``) stay
    bare; ``now()`` is a function call; anything else becomes a string
    literal so a stray token can't break out of the query.
    """
    v = (value or "").strip()
    if not v:
        return "-5m"
    if v == "now()" or v.startswith("-") or v[:1].isdigit():
        return v
    return f'"{_escape_flux_string(v)}"'


def parse_query(params, *, default_limit: int, max_limit: int) -> HistoryQuery:
    """Pull ``HistoryQuery`` out of an aiohttp query-string view.

    The aggressive validation here is intentional: this endpoint sits on
    a non-public port but still receives center input that ultimately
    flows from the browser, so we treat it as untrusted and enforce
    bounds up-front rather than relying on Flux to fail on bad input.
    """
    raw_points = params.get("point_ids") or ""
    point_ids = [p.strip() for p in raw_points.split(",") if p.strip()]
    if not point_ids:
        raise HistoryError(400, "missing_point_ids",
                           "point_ids query parameter is required")

    if len(point_ids) > 200:
        raise HistoryError(400, "too_many_point_ids",
                           "point_ids capped at 200 per request")

    agg = (params.get("agg") or AGG_RAW).strip()
    if agg not in _ALLOWED_AGG:
        raise HistoryError(
            400, "bad_agg",
            f"agg must be one of {sorted(_ALLOWED_AGG)}; got {agg!r}",
        )

    limit_raw = params.get("limit")
    if limit_raw is None or limit_raw == "":
        limit = default_limit
    else:
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            raise HistoryError(400, "bad_limit", "limit must be an integer")
        if limit <= 0:
            raise HistoryError(400, "bad_limit", "limit must be positive")
    # Hard cap — overrides whatever the caller asked for.
    if limit > max_limit:
        limit = max_limit

    start = params.get("start") or "-5m"
    end = params.get("end") or "now()"
    return HistoryQuery(
        point_ids=point_ids, start=start, end=end, agg=agg, limit=limit,
    )


def build_flux_query(
    q: HistoryQuery, *, bucket: str, max_limit: int,
) -> str:
    """Build the Flux query that backs ``/history/points``.

    Filters by ``_field`` ∈ point_ids — M5 made the point code the field
    key (not a tag), so this is the cheapest filter Influx can run. For
    ``agg=1s`` / ``agg=10s`` we add an ``aggregateWindow(mean)`` so the
    center can request pre-downsampled series for the 24 h drawer
    without pulling raw samples across the wire.
    """
    safe_filters = " or ".join(
        f'r["_field"] == "{_escape_flux_string(pid)}"' for pid in q.point_ids
    )
    start_arg = _flux_range_arg(q.start)
    end_arg = _flux_range_arg(q.end)
    parts = [
        f'from(bucket:"{_escape_flux_string(bucket)}")',
        f"|> range(start: {start_arg}, stop: {end_arg})",
        f"|> filter(fn: (r) => {safe_filters})",
    ]
    if q.agg == AGG_1S:
        parts.append('|> aggregateWindow(every: 1s, fn: mean, createEmpty: false)')
    elif q.agg == AGG_10S:
        parts.append('|> aggregateWindow(every: 10s, fn: mean, createEmpty: false)')
    parts.append('|> sort(columns: ["_time"])')
    # Always cap result size — protects the edge from a runaway query.
    parts.append(f"|> limit(n: {min(q.limit, max_limit)})")
    return " ".join(parts)


def _record_value(value):
    """Normalise an Influx value into JSON-serialisable shape."""
    if value is None:
        return None
    if isinstance(value, (int, float, bool)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def _format_records(records: Iterable[dict]) -> list[dict]:
    """Project Flux records into the wire shape the center proxy returns."""
    out: list[dict] = []
    for rec in records:
        ts = rec.get("_time")
        if ts is None:
            continue
        ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        out.append({
            "point_code": rec.get("_field") or rec.get("point_code"),
            "timestamp": ts_str,
            "value": _record_value(rec.get("_value")),
            "quality": rec.get("quality") or "good",
        })
    return out


class HistoryServer:
    """aiohttp app that serves ``/history/points`` on the edge.

    The InfluxDB connection is lazily established on the first request and
    reused across calls — querying is cheap, but ``connect()`` performs a
    network round-trip we'd rather not pay per-request.

    ``storage_factory`` is injected (default: build the real
    :class:`storage.influxdb.InfluxDBStorage` from Django settings) so
    tests can hand in a fake without spinning up Influx.
    """

    DEFAULT_PORT = 18086
    DEFAULT_HOST = "0.0.0.0"
    DEFAULT_MAX_POINTS = 50_000

    def __init__(
        self,
        config: EdgeConfig,
        *,
        host: Optional[str] = None,
        port: Optional[int] = None,
        max_points: Optional[int] = None,
        default_limit: int = 5_000,
        storage_factory=None,
    ) -> None:
        self.config = config
        self.host = host or config.history_host
        self.port = port or config.history_port
        self.max_points = max_points or config.history_max_points
        self.default_limit = default_limit
        self._storage_factory = storage_factory or self._build_default_storage
        self._storage = None
        self._runner: Optional[web.AppRunner] = None

    # ---- aiohttp lifecycle ------------------------------------------------

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/healthz", self._handle_healthz)
        app.router.add_get("/history/points", self._handle_history)
        return app

    async def start(self) -> None:
        """Bind the listener; safe to call once per agent lifetime."""
        app = self.build_app()
        self._runner = web.AppRunner(app, handle_signals=False)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info(
            "edge-agent: history HTTP listening on %s:%d (max_points=%d)",
            self.host, self.port, self.max_points,
        )

    async def stop(self) -> None:
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            finally:
                self._runner = None
        if self._storage is not None:
            try:
                self._storage.disconnect()
            except Exception:  # noqa: BLE001
                logger.exception("edge-agent: history storage disconnect failed")
            self._storage = None

    # ---- handlers ---------------------------------------------------------

    async def _handle_healthz(self, request: web.Request) -> web.Response:
        # Cheap liveness probe — used by docker-compose healthchecks and by
        # the center proxy to skip an offline edge without paying the Flux
        # query cost.
        return web.json_response({"status": "ok", "edge_id": self.config.edge_id})

    async def _handle_history(self, request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization") or ""
        if not auth.startswith("Bearer "):
            return self._error_response(401, "missing_token",
                                        "Authorization: Bearer <token> required")
        if auth[len("Bearer "):].strip() != self.config.edge_token:
            return self._error_response(401, "bad_token",
                                        "edge token rejected")

        try:
            q = parse_query(
                request.query,
                default_limit=self.default_limit,
                max_limit=self.max_points,
            )
        except HistoryError as exc:
            return self._error_response(exc.status, exc.code, exc.message)

        try:
            storage = self._ensure_storage()
        except Exception as exc:  # noqa: BLE001
            logger.exception("edge-agent: history storage unavailable")
            return self._error_response(
                503, "influx_unavailable",
                f"edge influx not reachable: {exc}",
            )

        bucket = getattr(storage, "bucket", "iot-data")
        flux = build_flux_query(q, bucket=bucket, max_limit=self.max_points)

        started = time.monotonic()
        try:
            # storage.query() is blocking — push it off the loop so a slow
            # Influx query doesn't stall the rest of the agent.
            import asyncio
            records = await asyncio.get_running_loop().run_in_executor(
                None, storage.query, flux,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("edge-agent: history flux query failed")
            return self._error_response(500, "query_failed", str(exc))

        elapsed_ms = int((time.monotonic() - started) * 1000)
        data = _format_records(records)
        # Capped → set a flag so the center can surface "truncated" UX.
        truncated = len(data) >= q.limit
        body = {
            "edge_id": self.config.edge_id,
            "point_ids": q.point_ids,
            "start": q.start,
            "end": q.end,
            "agg": q.agg,
            "limit": q.limit,
            "count": len(data),
            "truncated": truncated,
            "elapsed_ms": elapsed_ms,
            "data": data,
        }
        return web.json_response(body)

    # ---- helpers ----------------------------------------------------------

    def _ensure_storage(self):
        if self._storage is None:
            self._storage = self._storage_factory()
        return self._storage

    @staticmethod
    def _build_default_storage():
        """Construct the real InfluxDBStorage from Django settings.

        Imported lazily because storage/Django are only available after
        ``ensure_setup()`` has put ``backend`` on ``sys.path`` — the
        edge-agent does that during normal startup, but tests inject a
        fake factory and never need this branch.
        """
        from django.conf import settings as dj_settings
        from storage import StorageRegistry

        cfg = {
            "url": getattr(dj_settings, "INFLUXDB_URL", None),
            "host": getattr(dj_settings, "INFLUXDB_HOST", "localhost"),
            "port": getattr(dj_settings, "INFLUXDB_PORT", 8086),
            "token": getattr(dj_settings, "INFLUXDB_TOKEN", ""),
            "org": getattr(dj_settings, "INFLUXDB_ORG", "default"),
            "bucket": getattr(dj_settings, "INFLUXDB_BUCKET", "default"),
        }
        storage = StorageRegistry.create("influxdb", cfg)
        storage.connect()
        return storage

    @staticmethod
    def _error_response(status: int, code: str, message: str) -> web.Response:
        return web.json_response(
            {"error": {"code": code, "message": message}},
            status=status,
        )
