"""Tests for the M6 history proxy (XIU-83).

Center-side endpoint ``GET /api/history/points`` that:
* resolves ``task_id`` → ``AcqTask.edge`` and 503s if the edge is offline
* groups requested ``point_ids`` per edge, fan-outs in parallel
* surfaces per-edge errors keyed by ``edge_id`` so the UI can render
  "edge X offline" instead of an empty chart
* falls back to a direct center-Influx query when ``AcqTask.edge`` is
  None (legacy single-host deployment)
* does not touch the center's InfluxDB when every task is bound to an
  edge — this proves the "中心 InfluxDB 关掉,历史查询仍然工作" acceptance
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.conf import settings
from django.utils import timezone
from rest_framework.test import APIClient

from configuration.models import AcqTask
from fleet.models import EdgeNode, EdgeStatus

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_db_defaults():
    """Re-inject Django 4.2 connection defaults the session conftest drops."""
    db = settings.DATABASES["default"]
    db.setdefault("TIME_ZONE", None)
    db.setdefault("CONN_HEALTH_CHECKS", False)
    db.setdefault("CONN_MAX_AGE", 0)
    db.setdefault("AUTOCOMMIT", True)
    db.setdefault("OPTIONS", {})
    yield


@pytest.fixture(autouse=True)
def _proxy_token():
    """Make a single shared token resolve for every edge so the test path is
    not gated on out-of-band per-edge secrets."""
    settings.EDGE_HISTORY_PROXY_DEFAULT_TOKEN = "proxy-secret"
    yield


def _make_online_edge(name: str, *, history_url: str) -> EdgeNode:
    node = EdgeNode.objects.create(
        name=name,
        token_hash=EdgeNode.hash_token("dummy"),
        status=EdgeStatus.ONLINE,
        last_seen=timezone.now(),
        labels={"history_url": history_url},
    )
    return node


def _make_offline_edge(name: str) -> EdgeNode:
    return EdgeNode.objects.create(
        name=name,
        token_hash=EdgeNode.hash_token("dummy"),
        status=EdgeStatus.OFFLINE,
        # XIU-112: is_stale() is now status-driven; last_seen is diagnostic.
        last_seen=timezone.now() - timedelta(minutes=10),
        labels={"history_url": f"http://{name}:18086"},
    )


def _make_task(code: str, *, edge=None) -> AcqTask:
    return AcqTask.objects.create(code=code, name=code, edge=edge)


# ---------------------------------------------------------------------------
# Fake HTTP layer
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.text = text or ""

    def json(self):
        return self._json


def _ok_payload(edge_id: str, *, count: int = 3, point_id: str = "T1"):
    return {
        "edge_id": edge_id,
        "point_ids": [point_id],
        "start": "-5m",
        "end": "now()",
        "agg": "raw",
        "limit": 5000,
        "count": count,
        "truncated": False,
        "elapsed_ms": 4,
        "data": [
            {"point_code": point_id, "timestamp": f"2026-01-01T00:00:{i:02d}+00:00",
             "value": 20.0 + i, "quality": "good"}
            for i in range(count)
        ],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_history_proxy_requires_task_id():
    client = APIClient()
    resp = client.get("/api/history/points")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "missing_task_id"


def test_history_proxy_requires_point_ids():
    edge = _make_online_edge("edge-a", history_url="http://edge-a:18086")
    task = _make_task("T1", edge=edge)
    client = APIClient()
    resp = client.get(f"/api/history/points?task_id={task.id}")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "missing_point_ids"


def test_history_proxy_unknown_task_404():
    client = APIClient()
    resp = client.get("/api/history/points?task_id=999999&point_ids=p1")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "unknown_task"


def test_history_proxy_fan_out_to_single_edge():
    edge = _make_online_edge("edge-a", history_url="http://edge-a:18086")
    task = _make_task("T-a", edge=edge)
    client = APIClient()

    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _FakeResponse(200, _ok_payload("edge-a", count=3, point_id="T_temp"))

    with patch("history_proxy.views.requests.get", side_effect=fake_get):
        resp = client.get(
            f"/api/history/points?task_id={task.id}&point_ids=T_temp&start=-1h",
        )
    assert resp.status_code == 200
    body = resp.json()
    assert "edge-a" in body["sources"]
    assert body["sources"]["edge-a"]["count"] == 3
    # Each merged row was stamped with its source edge id.
    assert all(row["edge_id"] == "edge-a" for row in body["data"])
    # Center forwarded the Bearer token + point_ids + start window.
    assert captured["headers"]["Authorization"] == "Bearer proxy-secret"
    assert captured["params"]["point_ids"] == "T_temp"
    assert captured["params"]["start"] == "-1h"
    assert captured["url"] == "http://edge-a:18086/history/points"


def test_history_proxy_offline_edge_returns_503_without_calling_edge():
    edge = _make_offline_edge("edge-down")
    task = _make_task("T-down", edge=edge)
    client = APIClient()

    def boom(*a, **kw):  # noqa: D401
        raise AssertionError("offline edge must NOT be called over HTTP")

    with patch("history_proxy.views.requests.get", side_effect=boom):
        resp = client.get(
            f"/api/history/points?task_id={task.id}&point_ids=p1",
        )
    assert resp.status_code == 503
    body = resp.json()
    assert "edge-down" in body["errors"]
    assert body["errors"]["edge-down"]["code"] == "edge_offline"


def test_history_proxy_fan_out_across_two_edges_parallel():
    edge_a = _make_online_edge("edge-a", history_url="http://edge-a:18086")
    edge_b = _make_online_edge("edge-b", history_url="http://edge-b:18086")
    task_a = _make_task("T-a", edge=edge_a)
    task_b = _make_task("T-b", edge=edge_b)
    client = APIClient()

    seen_urls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        seen_urls.append(url)
        edge_name = "edge-a" if "edge-a" in url else "edge-b"
        return _FakeResponse(200, _ok_payload(edge_name, count=2, point_id="P"))

    with patch("history_proxy.views.requests.get", side_effect=fake_get):
        resp = client.get(
            f"/api/history/points?task_id={task_a.id},{task_b.id}&point_ids=P",
        )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["sources"].keys()) == {"edge-a", "edge-b"}
    assert body["count"] == 4
    # One call per edge — multi-task → same edge wouldn't double-dial.
    assert sorted(seen_urls) == [
        "http://edge-a:18086/history/points",
        "http://edge-b:18086/history/points",
    ]


def test_history_proxy_dedupes_calls_for_multiple_tasks_on_same_edge():
    """Two tasks bound to the *same* edge share a single HTTP call."""
    edge = _make_online_edge("edge-only", history_url="http://edge-only:18086")
    t1 = _make_task("T1", edge=edge)
    t2 = _make_task("T2", edge=edge)
    client = APIClient()

    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        return _FakeResponse(200, _ok_payload("edge-only", count=1, point_id="P"))

    with patch("history_proxy.views.requests.get", side_effect=fake_get):
        resp = client.get(
            f"/api/history/points?task_id={t1.id},{t2.id}&point_ids=P",
        )
    assert resp.status_code == 200
    # Only one HTTP call despite two tasks targeting the same edge.
    assert len(calls) == 1


def test_history_proxy_partial_failure_mixes_sources_and_errors():
    edge_ok = _make_online_edge("edge-ok", history_url="http://edge-ok:18086")
    edge_offline = _make_offline_edge("edge-stale")
    task_ok = _make_task("T-ok", edge=edge_ok)
    task_stale = _make_task("T-stale", edge=edge_offline)
    client = APIClient()

    def fake_get(url, params=None, headers=None, timeout=None):
        return _FakeResponse(200, _ok_payload("edge-ok", count=2, point_id="P"))

    with patch("history_proxy.views.requests.get", side_effect=fake_get):
        resp = client.get(
            f"/api/history/points?task_id={task_ok.id},{task_stale.id}&point_ids=P",
        )
    # Mixed success/failure → HTTP 200 with both sources + errors.
    assert resp.status_code == 200
    body = resp.json()
    assert "edge-ok" in body["sources"]
    assert "edge-stale" in body["errors"]
    assert body["errors"]["edge-stale"]["code"] == "edge_offline"


def test_history_proxy_edge_timeout_surfaces_504_in_errors():
    import requests as real_requests

    edge = _make_online_edge("edge-slow", history_url="http://edge-slow:18086")
    task = _make_task("T-slow", edge=edge)
    client = APIClient()

    def fake_get(url, params=None, headers=None, timeout=None):
        raise real_requests.Timeout("read timed out")

    with patch("history_proxy.views.requests.get", side_effect=fake_get):
        resp = client.get(
            f"/api/history/points?task_id={task.id}&point_ids=p1",
        )
    assert resp.status_code == 503  # every source failed
    body = resp.json()
    assert body["errors"]["edge-slow"]["code"] == "edge_timeout"
    assert body["errors"]["edge-slow"]["status"] == 504


def test_history_proxy_edge_unreachable_surfaces_in_errors():
    import requests as real_requests

    edge = _make_online_edge("edge-dead", history_url="http://edge-dead:18086")
    task = _make_task("T-dead", edge=edge)
    client = APIClient()

    def fake_get(url, params=None, headers=None, timeout=None):
        raise real_requests.ConnectionError("connection refused")

    with patch("history_proxy.views.requests.get", side_effect=fake_get):
        resp = client.get(
            f"/api/history/points?task_id={task.id}&point_ids=p1",
        )
    assert resp.status_code == 503
    body = resp.json()
    assert body["errors"]["edge-dead"]["code"] == "edge_unreachable"


def test_history_proxy_edge_auth_rejected_surfaces_502():
    edge = _make_online_edge("edge-401", history_url="http://edge-401:18086")
    task = _make_task("T-401", edge=edge)
    client = APIClient()

    def fake_get(url, params=None, headers=None, timeout=None):
        return _FakeResponse(401, {"error": {"code": "bad_token", "message": "no"}})

    with patch("history_proxy.views.requests.get", side_effect=fake_get):
        resp = client.get(
            f"/api/history/points?task_id={task.id}&point_ids=p1",
        )
    assert resp.status_code == 503
    body = resp.json()
    assert body["errors"]["edge-401"]["code"] == "edge_auth_rejected"


def test_history_proxy_does_not_touch_center_influx_when_edges_serve_request():
    """Acceptance: "中心 InfluxDB 关掉,历史查询仍然工作".

    We patch the legacy center-Influx fallback to raise — if the proxy
    ever reached for it while every task has an edge, the test would
    fail loudly instead of silently masking the regression.
    """
    edge = _make_online_edge("edge-iso", history_url="http://edge-iso:18086")
    task = _make_task("T-iso", edge=edge)
    client = APIClient()

    def fake_get(url, params=None, headers=None, timeout=None):
        return _FakeResponse(200, _ok_payload("edge-iso", count=4, point_id="P"))

    def explode_if_called(*a, **kw):
        raise AssertionError("center InfluxDB must NOT be queried in fleet mode")

    with patch("history_proxy.views.requests.get", side_effect=fake_get), \
         patch("history_proxy.views._query_center_legacy", side_effect=explode_if_called):
        resp = client.get(
            f"/api/history/points?task_id={task.id}&point_ids=P",
        )
    assert resp.status_code == 200


def test_history_proxy_edge_url_resolution_prefers_labels():
    """Center reads ``EdgeNode.labels['history_url']`` first, env override second."""
    edge = _make_online_edge(
        "edge-r", history_url="http://from-labels:18086",
    )
    task = _make_task("T-r", edge=edge)
    client = APIClient()

    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured["url"] = url
        return _FakeResponse(200, _ok_payload("edge-r", count=1, point_id="P"))

    # Operator override is present but the label wins.
    settings.EDGE_HISTORY_PROXY_URLS = {"edge-r": "http://from-override:18086"}
    try:
        with patch("history_proxy.views.requests.get", side_effect=fake_get):
            resp = client.get(
                f"/api/history/points?task_id={task.id}&point_ids=P",
            )
    finally:
        settings.EDGE_HISTORY_PROXY_URLS = {}
    assert resp.status_code == 200
    assert captured["url"].startswith("http://from-labels:18086")


def test_history_proxy_missing_edge_url_returns_503():
    """Edge online but did not advertise a history_url and no override → 503."""
    edge = EdgeNode.objects.create(
        name="edge-noaddr",
        token_hash=EdgeNode.hash_token("dummy"),
        status=EdgeStatus.ONLINE,
        last_seen=timezone.now(),
        labels={},  # no history_url
    )
    task = _make_task("T-noaddr", edge=edge)
    client = APIClient()

    resp = client.get(
        f"/api/history/points?task_id={task.id}&point_ids=p1",
    )
    assert resp.status_code == 503
    body = resp.json()
    assert body["errors"]["edge-noaddr"]["code"] == "edge_url_unknown"
