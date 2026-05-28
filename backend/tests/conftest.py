"""Pytest configuration and shared fixtures."""
import os
import sys
from pathlib import Path

import pytest
from django.conf import settings

# Add backend directory to Python path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

# Configure Django settings for tests
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "control_plane.settings")

import django
django.setup()


@pytest.fixture(scope="session")
def django_db_modify_db_settings():
    """Modify database settings to use file-based SQLite for tests."""
    import tempfile
    import os

    test_db_path = os.path.join(tempfile.gettempdir(), "test_edge_iot.db")

    settings.DATABASES["default"] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": test_db_path,
        "ATOMIC_REQUESTS": False,
        "CONN_MAX_AGE": 0,
        "OPTIONS": {
            "timeout": 30,
        },
    }
    return settings.DATABASES["default"]


@pytest.fixture(autouse=True)
def _default_fleet_transport_ws(request):
    """Pin ``FLEET_TRANSPORT=ws`` for legacy fleet tests (XIU-101 Phase 2 P2).

    Production default flipped to ``mqtt`` in XIU-101, but the existing M2/M4
    WebSocket-assertion tests were written against the WS dispatch path and
    have no MQTT broker available. Tests that exercise the new dispatcher
    set the transport explicitly via monkeypatch, so they override this
    fixture; the rest get the historically-correct WS behavior without an
    accidental paho connect storm against ``center-mosquitto``.
    """
    # Tests under tests/transport/test_mqtt_downlink.py manage the transport
    # mode themselves, so do not override for that module.
    if "test_mqtt_downlink" in request.node.nodeid:
        yield
        return
    original = getattr(settings, "FLEET_TRANSPORT", "mqtt")
    settings.FLEET_TRANSPORT = "ws"
    try:
        yield
    finally:
        settings.FLEET_TRANSPORT = original


@pytest.fixture(scope="session", autouse=True)
def celery_eager_env():
    """Configure Celery to execute tasks synchronously for all tests."""
    import os
    os.environ["CELERY_TASK_ALWAYS_EAGER"] = "True"
    # Reload celery app to pick up the new setting
    from celery import current_app
    current_app.conf.CELERY_TASK_ALWAYS_EAGER = True
    current_app.conf.CELERY_TASK_EAGER_PROPAGATES = True
    yield
    current_app.conf.CELERY_TASK_ALWAYS_EAGER = False
    current_app.conf.CELERY_TASK_EAGER_PROPAGATES = False


@pytest.fixture
def sample_device_config():
    """Sample device configuration for testing."""
    return {
        "source_ip": "192.168.1.100",
        "source_port": 502,
        "_test_simulated_data": {
            "POINT_001": 100,
            "POINT_002": 200,
            "POINT_003": 300,
        }
    }


@pytest.fixture
def sample_points_config():
    """Sample points configuration for testing."""
    return [
        {"code": "POINT_001", "address": "D100", "extra": {"type": 3, "num": 1}},
        {"code": "POINT_002", "address": "D101", "extra": {"type": 3, "num": 1}},
        {"code": "POINT_003", "address": "D102", "extra": {"type": 3, "num": 1}},
    ]


@pytest.fixture
def sample_storage_config():
    """Sample storage configuration for testing."""
    return {
        "url": "http://localhost:8086",
        "token": "test-token",
        "org": "test-org",
        "bucket": "test-bucket",
    }
