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
def django_db_setup(django_db_blocker):
    """Setup test database and create tables."""
    from django.core.management import call_command

    settings.DATABASES["default"] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }

    with django_db_blocker.unblock():
        call_command("migrate", "--run-syncdb", verbosity=0)


@pytest.fixture
def celery_eager():
    """Configure Celery to execute tasks synchronously."""
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.CELERY_TASK_EAGER_PROPAGATES = True
    yield
    settings.CELERY_TASK_ALWAYS_EAGER = False
    settings.CELERY_TASK_EAGER_PROPAGATES = False


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
