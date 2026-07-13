"""Security / correctness regression tests.

Covers four backend hardening fixes:
  1. Flux injection guard on the point-history endpoint.
  2. GlobalAcquisitionConsumer.get_active_sessions returning RUNNING + PAUSED.
  3. SSRF guard on the connection/storage test endpoints (common.network).
  4. Production settings safety guards (DEBUG=False refuses dev SECRET_KEY /
     permissive ALLOWED_HOSTS).
"""
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from asgiref.sync import async_to_sync
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APIClient

from acquisition import models as acq_models
from acquisition.consumers import GlobalAcquisitionConsumer
from acquisition.views import _validate_flux_time_arg
from common import network
from tests.fixtures.factories import *  # noqa: F401,F403

BACKEND_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture
def api_client():
    return APIClient()


# ---------------------------------------------------------------------------
# 1. Flux injection guard
# ---------------------------------------------------------------------------


class TestFluxTimeValidation:
    @pytest.mark.parametrize("value", [
        "-1h", "-7d", "30m", "-500ms", "10s", "-1w",
        "2025-10-10T00:00:00Z",
        "2025-10-10T00:00:00.123Z",
        "2025-10-10T00:00:00+08:00",
        "now()",
    ])
    def test_valid_inputs_pass_through_unchanged(self, value):
        assert _validate_flux_time_arg(value) == value

    @pytest.mark.parametrize("payload", [
        # Classic Flux injection: close range() and inject arbitrary pipeline.
        '-1h) |> drop(columns: ["_value"]) |> range(start: -1h',
        '2025-01-01T00:00:00Z) |> yield(name: "x"',
        "1h; import \"http\"",
        "now()) |> limit(n: 1)",
        "-1hh",
        "foobar",
        "",
        "   ",
        "-1",           # bare number, no unit
        "2025-10-10",   # date only, not RFC3339
    ])
    def test_injection_and_malformed_rejected(self, payload):
        with pytest.raises(ValueError):
            _validate_flux_time_arg(payload)

    @pytest.mark.django_db
    def test_point_history_rejects_injection_with_400(self, api_client):
        resp = api_client.get(
            "/api/acquisition/sessions/point-history/",
            {
                "point_code": "Temp_01",
                "start_time": '-1h) |> drop(columns: ["_value"]) |> range(start: -1h',
                "end_time": "now()",
            },
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    @pytest.mark.django_db
    def test_point_history_accepts_valid_range(self, api_client):
        # Patch storage so we exercise validation without a live InfluxDB.
        with patch("storage.StorageRegistry.create") as mock_create:
            mock_storage = MagicMock()
            mock_storage.query.return_value = []
            mock_create.return_value = mock_storage
            resp = api_client.get(
                "/api/acquisition/sessions/point-history/",
                {"point_code": "Temp_01", "start_time": "-2h", "end_time": "now()"},
            )
        assert resp.status_code == status.HTTP_200_OK


# ---------------------------------------------------------------------------
# 2. Active-sessions set (RUNNING + PAUSED)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestActiveSessions:
    def test_includes_running_and_paused_excludes_stopped(self, create_session):
        running = create_session(status=acq_models.AcquisitionSession.STATUS_RUNNING)
        paused = create_session(status=acq_models.AcquisitionSession.STATUS_PAUSED)
        create_session(status=acq_models.AcquisitionSession.STATUS_STOPPED)
        create_session(status=acq_models.AcquisitionSession.STATUS_ERROR)

        consumer = GlobalAcquisitionConsumer()
        rows = async_to_sync(consumer.get_active_sessions)()

        ids = {r["session_id"] for r in rows}
        assert ids == {running.id, paused.id}


# ---------------------------------------------------------------------------
# 3. SSRF guard
# ---------------------------------------------------------------------------


class TestSSRFHelper:
    def test_blocked_ips(self):
        import ipaddress
        for addr in ["127.0.0.1", "169.254.169.254", "10.0.0.5",
                     "172.16.3.4", "192.168.1.1", "0.0.0.0", "::1"]:
            assert network.is_blocked_ip(ipaddress.ip_address(addr)), addr

    def test_public_ips_allowed(self):
        import ipaddress
        for addr in ["8.8.8.8", "1.1.1.1", "93.184.216.34"]:
            assert not network.is_blocked_ip(ipaddress.ip_address(addr)), addr

    @override_settings(ALLOW_PRIVATE_NETWORK_TESTS=False)
    def test_config_target_metadata_endpoint_blocked(self):
        with pytest.raises(network.PrivateNetworkNotAllowed):
            network.assert_config_targets_allowed(
                {"source_ip": "169.254.169.254", "source_port": 80}
            )

    @override_settings(ALLOW_PRIVATE_NETWORK_TESTS=False)
    def test_config_target_url_private_blocked(self):
        with pytest.raises(network.PrivateNetworkNotAllowed):
            network.assert_config_targets_allowed({"url": "http://127.0.0.1:8086"})

    @override_settings(ALLOW_PRIVATE_NETWORK_TESTS=True)
    def test_allowed_flag_disables_guard(self):
        # No exception even for a loopback target when the flag is on.
        network.assert_config_targets_allowed({"source_ip": "127.0.0.1"})


@pytest.mark.django_db
class TestSSRFEndpoints:
    @override_settings(ALLOW_PRIVATE_NETWORK_TESTS=False)
    def test_connection_test_blocks_private_target(self, api_client):
        resp = api_client.post(
            "/api/acquisition/connection-tests/",
            {"protocol_type": "modbustcp",
             "device_config": {"source_ip": "169.254.169.254", "source_port": 502}},
            format="json",
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    @override_settings(ALLOW_PRIVATE_NETWORK_TESTS=False)
    def test_storage_test_blocks_private_target(self, api_client):
        resp = api_client.post(
            "/api/acquisition/storage-tests/",
            {"storage_type": "influxdb",
             "storage_config": {"url": "http://10.0.0.9:8086", "token": "x",
                                "org": "o", "bucket": "b"}},
            format="json",
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# 4. Production settings safety guards
# ---------------------------------------------------------------------------


def _import_settings(extra_env):
    env = {k: v for k, v in os.environ.items()}
    # Ensure no stray .env-provided overrides leak the real secret in.
    env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-c", "import control_plane.settings"],
        env=env,
        cwd=str(BACKEND_DIR),
        capture_output=True,
        text=True,
    )


class TestProductionSettingsGuard:
    def test_prod_with_dev_secret_key_raises(self):
        result = _import_settings({
            "DEBUG": "False",
            "SECRET_KEY": "dev-secret-key-change-me",
            "ALLOWED_HOSTS": "example.com",
        })
        assert result.returncode != 0
        assert "SECRET_KEY" in result.stderr

    def test_prod_with_empty_allowed_hosts_raises(self):
        result = _import_settings({
            "DEBUG": "False",
            "SECRET_KEY": "a-real-strong-secret",
            "ALLOWED_HOSTS": "",
        })
        assert result.returncode != 0
        assert "ALLOWED_HOSTS" in result.stderr

    def test_prod_with_proper_config_imports(self):
        result = _import_settings({
            "DEBUG": "False",
            "SECRET_KEY": "a-real-strong-secret",
            "ALLOWED_HOSTS": "example.com,api.example.com",
        })
        assert result.returncode == 0, result.stderr

    def test_dev_defaults_still_work(self):
        # DEBUG defaults to True, dev SECRET_KEY + permissive hosts allowed.
        result = _import_settings({
            "DEBUG": "True",
            "SECRET_KEY": "dev-secret-key-change-me",
            "ALLOWED_HOSTS": "",
        })
        assert result.returncode == 0, result.stderr
