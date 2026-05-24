"""Unit tests for the M6 read-only history HTTP server (XIU-83).

The endpoint sits in front of the local InfluxDB so the center proxy can
pull samples on demand. Tests cover the wire contract:

* token auth — 401 on missing / wrong / mistyped Bearer
* parameter validation — missing point_ids, bad agg, bad limit
* result-cap enforcement (``HISTORY_MAX_POINTS``)
* aggregation window threading through into the Flux query
* successful query → JSON shape with edge_id / count / data
* storage failures → 503 (Influx unavailable) / 500 (query crash)

The InfluxDB layer is stubbed via the ``storage_factory`` injection
point on :class:`HistoryServer` so the suite doesn't need a real Influx.
"""
from __future__ import annotations

import datetime as dt
from typing import List

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from edge_agent.config import EdgeConfig
from edge_agent.history_server import (
    AGG_1S,
    AGG_10S,
    HistoryServer,
    build_flux_query,
    parse_query,
    HistoryError,
)


# ---------------------------------------------------------------------------
# Stub storage — captures the Flux query so tests can assert the wire shape.
# ---------------------------------------------------------------------------


class _StubStorage:
    """Minimal stand-in for ``storage.influxdb.InfluxDBStorage``."""

    def __init__(self, records=None, *, bucket: str = "iot-data", raise_on=None):
        self.records = records or []
        self.bucket = bucket
        self.queries: List[str] = []
        self._raise_on = raise_on

    def query(self, flux: str):  # noqa: D401
        self.queries.append(flux)
        if self._raise_on:
            raise self._raise_on
        return self.records

    def disconnect(self):
        pass


def _make_records(n: int, *, point_code: str = "T1"):
    out = []
    t0 = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    for i in range(n):
        out.append({
            "_time": t0 + dt.timedelta(seconds=i),
            "_value": 20.0 + i,
            "_field": point_code,
            "quality": "good",
        })
    return out


def _make_config(**overrides) -> EdgeConfig:
    base = {
        "edge_id": "edge-test-1",
        "edge_token": "secret-token",
        "center_url": "ws://center/",
        "labels": {},
        "log_level": "INFO",
        "history_enabled": True,
        "history_host": "127.0.0.1",
        "history_port": 0,
        "history_max_points": 100,
        "history_url": "http://edge-test-1:18086",
    }
    base.update(overrides)
    return EdgeConfig(**base)


# ---------------------------------------------------------------------------
# Pure-function tests — parse / build_flux_query
# ---------------------------------------------------------------------------


def test_parse_query_requires_point_ids():
    with pytest.raises(HistoryError) as exc:
        parse_query({}, default_limit=100, max_limit=1000)
    assert exc.value.status == 400
    assert exc.value.code == "missing_point_ids"


def test_parse_query_rejects_bad_agg():
    with pytest.raises(HistoryError) as exc:
        parse_query(
            {"point_ids": "p1", "agg": "1m"},
            default_limit=100, max_limit=1000,
        )
    assert exc.value.code == "bad_agg"


def test_parse_query_clamps_limit_to_max():
    q = parse_query(
        {"point_ids": "p1", "limit": "999999"},
        default_limit=100, max_limit=500,
    )
    assert q.limit == 500


def test_parse_query_caps_point_ids():
    too_many = ",".join(f"p{i}" for i in range(300))
    with pytest.raises(HistoryError) as exc:
        parse_query({"point_ids": too_many}, default_limit=100, max_limit=1000)
    assert exc.value.code == "too_many_point_ids"


def test_build_flux_query_handles_agg_and_escapes():
    q = parse_query(
        {"point_ids": 'p1,p"weird', "start": "-1h", "end": "now()", "agg": "1s",
         "limit": "200"},
        default_limit=100, max_limit=500,
    )
    flux = build_flux_query(q, bucket="iot-data", max_limit=500)
    # Aggregation window threaded through.
    assert "aggregateWindow(every: 1s" in flux
    # Range arguments stay bare (not quoted).
    assert "range(start: -1h, stop: now())" in flux
    # Double-quote inside point id is escaped (no broken Flux literal).
    assert r'r["_field"] == "p\"weird"' in flux
    # Hard cap clamps the limit.
    assert "limit(n: 200)" in flux


def test_build_flux_query_10s_aggregation():
    q = parse_query(
        {"point_ids": "p1", "agg": "10s"}, default_limit=100, max_limit=500,
    )
    flux = build_flux_query(q, bucket="b", max_limit=500)
    assert "aggregateWindow(every: 10s" in flux


# ---------------------------------------------------------------------------
# HTTP-level tests — drive the aiohttp app through TestClient
# ---------------------------------------------------------------------------


@pytest.fixture
async def stub_storage():
    return _StubStorage(records=_make_records(5))


@pytest.fixture
async def history_client(stub_storage):
    cfg = _make_config()
    server = HistoryServer(
        cfg,
        max_points=50,
        default_limit=10,
        storage_factory=lambda: stub_storage,
    )
    app = server.build_app()
    async with TestClient(TestServer(app)) as client:
        yield client, server, stub_storage


async def test_healthz_does_not_require_token(history_client):
    client, _, _ = history_client
    resp = await client.get("/healthz")
    assert resp.status == 200
    body = await resp.json()
    assert body == {"status": "ok", "edge_id": "edge-test-1"}


async def test_history_rejects_missing_authorization(history_client):
    client, _, _ = history_client
    resp = await client.get("/history/points", params={"point_ids": "p1"})
    assert resp.status == 401
    body = await resp.json()
    assert body["error"]["code"] == "missing_token"


async def test_history_rejects_wrong_bearer_token(history_client):
    client, _, _ = history_client
    resp = await client.get(
        "/history/points",
        params={"point_ids": "p1"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status == 401
    body = await resp.json()
    assert body["error"]["code"] == "bad_token"


async def test_history_happy_path_returns_normalized_records(history_client):
    client, _, storage = history_client
    resp = await client.get(
        "/history/points",
        params={"point_ids": "T1", "start": "-5m", "end": "now()", "agg": "raw"},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["edge_id"] == "edge-test-1"
    assert body["point_ids"] == ["T1"]
    assert body["count"] == 5
    assert body["data"][0]["point_code"] == "T1"
    assert body["data"][0]["quality"] == "good"
    # ISO-8601 string, not a raw datetime.
    assert isinstance(body["data"][0]["timestamp"], str)
    assert body["data"][0]["timestamp"].endswith("+00:00")
    # The Flux query was actually issued.
    assert len(storage.queries) == 1
    assert 'r["_field"] == "T1"' in storage.queries[0]


async def test_history_caps_limit_at_max_points():
    """``HISTORY_MAX_POINTS`` overrides whatever the caller asked for."""
    storage = _StubStorage(records=_make_records(50))
    cfg = _make_config(history_max_points=10)
    server = HistoryServer(
        cfg, max_points=10, default_limit=5,
        storage_factory=lambda: storage,
    )
    async with TestClient(TestServer(server.build_app())) as client:
        resp = await client.get(
            "/history/points",
            params={"point_ids": "p1", "limit": "9999"},
            headers={"Authorization": "Bearer secret-token"},
        )
        assert resp.status == 200
        body = await resp.json()
        # Server-side cap is reflected in the response.
        assert body["limit"] == 10
        # And in the Flux query.
        assert "limit(n: 10)" in storage.queries[0]


async def test_history_truncated_flag_when_limit_hit():
    storage = _StubStorage(records=_make_records(5))
    cfg = _make_config()
    server = HistoryServer(
        cfg, max_points=50, default_limit=5,
        storage_factory=lambda: storage,
    )
    async with TestClient(TestServer(server.build_app())) as client:
        resp = await client.get(
            "/history/points",
            params={"point_ids": "T1", "limit": "5"},
            headers={"Authorization": "Bearer secret-token"},
        )
        body = await resp.json()
        assert body["count"] == 5
        assert body["truncated"] is True


async def test_history_storage_unavailable_returns_503():
    """``storage_factory`` raising → 503 instead of crashing the agent."""
    def boom():
        raise RuntimeError("influx is down")
    cfg = _make_config()
    server = HistoryServer(cfg, storage_factory=boom)
    async with TestClient(TestServer(server.build_app())) as client:
        resp = await client.get(
            "/history/points",
            params={"point_ids": "p1"},
            headers={"Authorization": "Bearer secret-token"},
        )
        assert resp.status == 503
        body = await resp.json()
        assert body["error"]["code"] == "influx_unavailable"


async def test_history_query_exception_returns_500():
    storage = _StubStorage(raise_on=RuntimeError("flux exploded"))
    cfg = _make_config()
    server = HistoryServer(cfg, storage_factory=lambda: storage)
    async with TestClient(TestServer(server.build_app())) as client:
        resp = await client.get(
            "/history/points",
            params={"point_ids": "p1"},
            headers={"Authorization": "Bearer secret-token"},
        )
        assert resp.status == 500
        body = await resp.json()
        assert body["error"]["code"] == "query_failed"
        assert "flux exploded" in body["error"]["message"]


async def test_history_rejects_bad_limit(history_client):
    client, _, _ = history_client
    resp = await client.get(
        "/history/points",
        params={"point_ids": "p1", "limit": "abc"},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["error"]["code"] == "bad_limit"


async def test_history_multiple_point_ids(history_client):
    client, _, storage = history_client
    resp = await client.get(
        "/history/points",
        params={"point_ids": "p1,p2,p3"},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert resp.status == 200
    # Filter must OR all three point ids.
    flux = storage.queries[0]
    assert 'r["_field"] == "p1"' in flux
    assert 'r["_field"] == "p2"' in flux
    assert 'r["_field"] == "p3"' in flux


# ---------------------------------------------------------------------------
# Config tests — env → EdgeConfig wiring for the new M6 fields
# ---------------------------------------------------------------------------


def test_history_config_advertises_default_url():
    """Edge advertises ``history_url`` in labels so the center can dial it."""
    from edge_agent.config import EdgeConfig

    cfg = EdgeConfig.from_env({
        "EDGE_ID": "edge-7",
        "EDGE_TOKEN": "tk",
        "CENTER_URL": "ws://c/",
    })
    assert cfg.history_enabled is True
    assert cfg.history_port == 18086
    assert cfg.history_url == "http://edge-7:18086"
    assert cfg.labels["history_url"] == "http://edge-7:18086"


def test_history_config_respects_operator_url():
    from edge_agent.config import EdgeConfig

    cfg = EdgeConfig.from_env({
        "EDGE_ID": "edge-7",
        "EDGE_TOKEN": "tk",
        "CENTER_URL": "ws://c/",
        "EDGE_HISTORY_URL": "http://10.0.0.5:18086",
        "EDGE_HISTORY_PORT": "29000",
        "EDGE_HISTORY_MAX_POINTS": "12345",
    })
    assert cfg.history_url == "http://10.0.0.5:18086"
    assert cfg.history_port == 29000
    assert cfg.history_max_points == 12345
    assert cfg.labels["history_url"] == "http://10.0.0.5:18086"


def test_history_config_disabled_skips_label_advert():
    from edge_agent.config import EdgeConfig

    cfg = EdgeConfig.from_env({
        "EDGE_ID": "edge-7",
        "EDGE_TOKEN": "tk",
        "CENTER_URL": "ws://c/",
        "EDGE_HISTORY_ENABLED": "false",
    })
    assert cfg.history_enabled is False
    assert "history_url" not in cfg.labels


def test_history_config_rejects_bad_port():
    from edge_agent.config import ConfigError, EdgeConfig

    with pytest.raises(ConfigError):
        EdgeConfig.from_env({
            "EDGE_ID": "e", "EDGE_TOKEN": "t", "CENTER_URL": "ws://c/",
            "EDGE_HISTORY_PORT": "abc",
        })
    with pytest.raises(ConfigError):
        EdgeConfig.from_env({
            "EDGE_ID": "e", "EDGE_TOKEN": "t", "CENTER_URL": "ws://c/",
            "EDGE_HISTORY_PORT": "999999",
        })
