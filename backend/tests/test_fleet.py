"""Tests for the fleet/ Django app (M1 — XIU-52)."""
from __future__ import annotations

from datetime import timedelta

import pytest
from asgiref.sync import sync_to_async
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.utils import timezone
from rest_framework.test import APIClient

from fleet.models import EdgeNode, EdgeStatus
from fleet.protocol import (
    PROTOCOL_VERSION,
    make_heartbeat,
    make_register,
)
from fleet.routing import websocket_urlpatterns


pytestmark = pytest.mark.django_db


# Bare URL router — skips AllowedHostsOriginValidator so WebsocketCommunicator
# (which doesn't send an Origin header by default) can complete the handshake.
fleet_application = URLRouter(websocket_urlpatterns)


@pytest.fixture(autouse=True)
def _patch_db_defaults():
    """The session-scoped `django_db_modify_db_settings` fixture in conftest
    replaces DATABASES["default"] with a hand-written dict that omits several
    Django 4.2 connection-config defaults (TIME_ZONE, CONN_HEALTH_CHECKS,
    CONN_MAX_AGE, etc.). Sync queries reuse a pre-existing connection so they
    don't trip — but `database_sync_to_async` opens fresh connections in
    worker threads, and `BaseDatabaseWrapper.connect()` raises KeyError on the
    missing keys. We re-inject the defaults locally so the global fixture
    keeps working for everyone else."""
    db = settings.DATABASES["default"]
    db.setdefault("TIME_ZONE", None)
    db.setdefault("CONN_HEALTH_CHECKS", False)
    db.setdefault("CONN_MAX_AGE", 0)
    db.setdefault("AUTOCOMMIT", True)
    db.setdefault("OPTIONS", {})
    yield


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class TestEdgeNodeModel:
    def test_issue_returns_token_and_hashes_it(self):
        node, token = EdgeNode.issue(name="edge-1")

        assert node.status == EdgeStatus.PENDING
        # Plaintext token is NOT persisted; only the hash is.
        assert token
        assert token != node.token_hash
        assert node.token_hash == EdgeNode.hash_token(token)
        assert node.verify_token(token) is True
        assert node.verify_token(token + "x") is False

    def test_mark_online_sets_status_and_last_seen(self):
        node, _ = EdgeNode.issue(name="edge-2")
        assert node.last_seen is None

        node.mark_online(version="0.1.0")

        node.refresh_from_db()
        assert node.status == EdgeStatus.ONLINE
        assert node.version == "0.1.0"
        assert node.last_seen is not None

    def test_is_stale_tracks_status_not_last_seen(self):
        """XIU-112: is_stale() is lwt/status-driven, with NO last_seen decay.

        An ONLINE edge is never stale regardless of how old last_seen is —
        an idle edge sends no uplink but stays connected via MQTT keepalive
        and its retained lwt is still ``online``. Only an OFFLINE status
        (set by the broker's last-will via apply_lwt) makes it stale.
        """
        node, _ = EdgeNode.issue(name="edge-3")
        # PENDING (never connected) is not "stale" — it has no presence yet.
        assert node.is_stale() is False

        # ONLINE with a very old last_seen must STILL be non-stale.
        node.mark_online()
        node.last_seen = timezone.now() - timedelta(hours=2)
        assert node.is_stale() is False

        # Only an explicit OFFLINE (lwt last-will) is stale.
        node.status = EdgeStatus.OFFLINE
        assert node.is_stale() is True


# ---------------------------------------------------------------------------
# Presence (lwt single source of truth — XIU-112) / REST API
# ---------------------------------------------------------------------------


class TestPresenceAndAPI:
    """XIU-112: status is driven solely by the broker's retained lwt topic.

    There is no longer a ``last_seen`` time-decay sweep — an ONLINE edge
    stays online no matter how stale ``last_seen`` is, and the list
    endpoint never silently flips it to offline. Offline is only ever set
    by the broker's last-will via :func:`fleet.presence.apply_lwt`.
    """

    def test_idle_online_edge_does_not_decay_to_offline(self):
        """lwt=online then long idle (no uplink) → status stays ONLINE.

        Reproduces the CEO's XIU-51 demo bug: pre-fix, an edge with a
        ``last_seen`` older than 30s was swept to offline on the next
        list() even though its retained lwt was still ``online``.
        """
        node, _ = EdgeNode.issue(name="idle-edge")
        node.mark_online()
        # Backdate last_seen far past the old 30s window — must NOT matter.
        EdgeNode.objects.filter(pk=node.pk).update(
            last_seen=timezone.now() - timedelta(minutes=10),
        )

        client = APIClient()
        resp = client.get("/api/fleet/edges/")
        assert resp.status_code == 200

        data = resp.json()
        if isinstance(data, dict) and "results" in data:
            data = data["results"]
        # Filter to this test's edge — the file-based test DB is shared
        # across modules so other suites' edges may also be present.
        rows = [e for e in data if e["name"] == "idle-edge"]
        assert len(rows) == 1
        assert rows[0]["status"] == "online"

    def test_offline_edge_listed_as_offline(self):
        """An edge the broker last-will marked offline is reported offline.

        The list endpoint reflects whatever ``status`` presence wrote — it
        no longer computes offline itself.
        """
        node, _ = EdgeNode.issue(name="downed-edge")
        node.mark_online()
        # Simulate the broker last-will path having set OFFLINE.
        EdgeNode.objects.filter(pk=node.pk).update(status=EdgeStatus.OFFLINE)

        client = APIClient()
        resp = client.get("/api/fleet/edges/")
        assert resp.status_code == 200

        data = resp.json()
        if isinstance(data, dict) and "results" in data:
            data = data["results"]
        rows = [e for e in data if e["name"] == "downed-edge"]
        assert len(rows) == 1
        assert rows[0]["status"] == "offline"

    def test_create_endpoint_returns_one_shot_token(self):
        client = APIClient()
        resp = client.post(
            "/api/fleet/edges/",
            data={"name": "factory-1", "labels": {"site": "sh"}},
            format="json",
        )
        assert resp.status_code == 201
        body = resp.json()

        assert body["name"] == "factory-1"
        assert body["status"] == "pending"
        assert "activation_token" in body and body["activation_token"]

        node = EdgeNode.objects.get(name="factory-1")
        assert node.verify_token(body["activation_token"])
        # Plaintext token is NOT echoed back on a subsequent GET.
        detail = client.get(f"/api/fleet/edges/{node.pk}/").json()
        assert "activation_token" not in detail


# ---------------------------------------------------------------------------
# Consumer — register + heartbeat over an in-memory WS
# ---------------------------------------------------------------------------


@pytest.fixture
def _inmemory_channel_layer():
    """Swap the Redis channel layer for an in-memory one for WS tests.

    Since M2 (XIU-59) the FleetConsumer joins a per-edge channel group on
    register; without this the consumer test would need a live Redis. We
    reset the cached layer registry so each test gets a fresh layer.
    """
    from channels.layers import channel_layers

    original = settings.CHANNEL_LAYERS
    settings.CHANNEL_LAYERS = {
        "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
    }
    channel_layers.backends = {}
    yield
    settings.CHANNEL_LAYERS = original
    channel_layers.backends = {}


@pytest.mark.asyncio
@pytest.mark.usefixtures("_inmemory_channel_layer")
class TestFleetConsumer:
    async def test_register_then_heartbeat_marks_edge_online(self, django_db_blocker):
        from asgiref.sync import sync_to_async

        with django_db_blocker.unblock():
            node, token = await sync_to_async(EdgeNode.issue)(name="ws-edge")

        comm = WebsocketCommunicator(fleet_application, "/ws/fleet/")
        connected, _ = await comm.connect()
        assert connected

        await comm.send_json_to(make_register(
            edge_id="ws-edge", token=token, version="0.1.0", labels={"site": "test"},
        ))
        ack = await comm.receive_json_from()
        assert ack["type"] == "ack"
        assert ack["ref"] == "register"
        assert ack["v"] == PROTOCOL_VERSION

        await comm.send_json_to(make_heartbeat(edge_id="ws-edge", uptime=1.0))
        ack2 = await comm.receive_json_from()
        assert ack2["type"] == "ack"
        assert ack2["ref"] == "heartbeat"

        await comm.disconnect()

        @sync_to_async
        def _reload():
            node.refresh_from_db()
            return node

        reloaded = await _reload()
        assert reloaded.status == EdgeStatus.ONLINE
        assert reloaded.version == "0.1.0"
        assert reloaded.labels == {"site": "test"}
        assert reloaded.last_seen is not None

    async def test_register_with_bad_token_returns_error_and_closes(self, django_db_blocker):
        from asgiref.sync import sync_to_async

        with django_db_blocker.unblock():
            await sync_to_async(EdgeNode.issue)(name="auth-edge")

        comm = WebsocketCommunicator(fleet_application, "/ws/fleet/")
        connected, _ = await comm.connect()
        assert connected

        await comm.send_json_to(make_register(
            edge_id="auth-edge", token="wrong-token", version="0.1.0",
        ))
        err = await comm.receive_json_from()
        assert err["type"] == "error"
        assert err["code"] == "auth_failed"

        # Center closes; reading after close should yield disconnect.
        close = await comm.receive_output(timeout=2)
        assert close["type"] == "websocket.close"

    async def test_unknown_edge_returns_error(self):
        comm = WebsocketCommunicator(fleet_application, "/ws/fleet/")
        connected, _ = await comm.connect()
        assert connected

        await comm.send_json_to(make_register(
            edge_id="nope", token="anything", version="0.1.0",
        ))
        err = await comm.receive_json_from()
        assert err["type"] == "error"
        assert err["code"] == "unknown_edge"

    async def test_heartbeat_before_register_is_rejected(self):
        comm = WebsocketCommunicator(fleet_application, "/ws/fleet/")
        connected, _ = await comm.connect()
        assert connected

        await comm.send_json_to(make_heartbeat(edge_id="anything"))
        err = await comm.receive_json_from()
        assert err["type"] == "error"
        assert err["code"] == "bad_frame"
