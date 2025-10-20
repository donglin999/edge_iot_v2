"""Factory functions for creating test data."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List
import itertools

import pytest
from django.utils import timezone

from acquisition import models as acq_models
from configuration import models as config_models


# Counters to ensure unique codes
_site_counter = itertools.count(1)
_device_counter = itertools.count(1)
_point_counter = itertools.count(1)
_task_counter = itertools.count(1)
_template_counter = itertools.count(1)


@pytest.fixture
def create_site():
    """Factory for creating Site instances."""
    def _create(code: str = None, name: str = None, **kwargs):
        if code is None:
            code = f"TEST_SITE_{next(_site_counter)}"
        if name is None:
            name = f"Test Site {code}"
        return config_models.Site.objects.create(
            code=code,
            name=name,
            description=kwargs.get("description", "Test site description"),
        )
    return _create


@pytest.fixture
def create_device(create_site):
    """Factory for creating Device instances."""
    def _create(
        site: config_models.Site = None,
        protocol: str = "mock_modbus",
        ip: str = "192.168.1.100",
        port: int = 502,
        **kwargs
    ):
        if site is None:
            site = create_site()

        code = kwargs.get("code")
        if code is None:
            code = f"DEVICE_{next(_device_counter)}"

        return config_models.Device.objects.create(
            site=site,
            name=kwargs.get("name", f"Test Device {code}"),
            code=code,
            protocol=protocol,
            ip_address=ip,
            port=port,
            metadata=kwargs.get("metadata", {}),
        )
    return _create


@pytest.fixture
def create_point_template():
    """Factory for creating PointTemplate instances."""
    def _create(name: str = None, **kwargs):
        if name is None:
            name = f"TEMPLATE_{next(_template_counter)}"
        return config_models.PointTemplate.objects.create(
            name=kwargs.get("cn_name", f"测点_{name}"),
            english_name=name,
            unit=kwargs.get("unit", "°C"),
            data_type=kwargs.get("data_type", "float"),
            coefficient=Decimal(kwargs.get("coefficient", "1.0")),
            precision=kwargs.get("precision", 2),
        )
    return _create


@pytest.fixture
def create_point(create_device, create_point_template):
    """Factory for creating Point instances."""
    def _create(
        device: config_models.Device = None,
        template: config_models.PointTemplate = None,
        code: str = None,
        **kwargs
    ):
        if device is None:
            device = create_device()

        if code is None:
            code = f"POINT_{next(_point_counter)}"

        if template is None and kwargs.get("create_template", True):
            template = create_point_template(name=code)

        return config_models.Point.objects.create(
            device=device,
            template=template,
            code=code,
            address=kwargs.get("address", "D100"),
            description=kwargs.get("description", f"Test point {code}"),
            sample_rate_hz=Decimal(kwargs.get("sample_rate_hz", "1.0")),
            to_kafka=kwargs.get("to_kafka", False),
            extra=kwargs.get("extra", {"type": "int16", "num": 1}),
        )
    return _create


@pytest.fixture
def create_task(create_point):
    """Factory for creating AcqTask instances."""
    def _create(
        code: str = None,
        points: List[config_models.Point] = None,
        **kwargs
    ):
        if code is None:
            code = f"TASK_{next(_task_counter)}"

        task = config_models.AcqTask.objects.create(
            code=code,
            name=kwargs.get("name", f"Test Task {code}"),
            description=kwargs.get("description", "Test acquisition task"),
            schedule=kwargs.get("schedule", "continuous"),
            is_active=kwargs.get("is_active", True),
        )

        # Add points to task
        if points is None:
            # Create default points
            points = [create_point() for i in range(3)]

        task.points.set(points)
        return task

    return _create


@pytest.fixture
def create_session(create_task):
    """Factory for creating AcquisitionSession instances."""
    def _create(
        task: config_models.AcqTask = None,
        status: str = acq_models.AcquisitionSession.STATUS_RUNNING,
        **kwargs
    ):
        if task is None:
            task = create_task()

        return acq_models.AcquisitionSession.objects.create(
            task=task,
            status=status,
            celery_task_id=kwargs.get("celery_task_id", "test-task-id"),
            started_at=kwargs.get("started_at", timezone.now()),
            stopped_at=kwargs.get("stopped_at"),
            error_message=kwargs.get("error_message", ""),
            metadata=kwargs.get("metadata", {}),
        )

    return _create


@pytest.fixture
def sample_device_config():
    """Sample device configuration for testing."""
    return {
        "source_ip": "192.168.1.100",
        "source_port": 502,
        "protocol_type": "mock_modbus",
        "_test_simulated_data": {
            "POINT_001": 100,
            "POINT_002": 200,
            "POINT_003": 300,
        },
    }


@pytest.fixture
def sample_points_config():
    """Sample points configuration for testing."""
    return [
        {
            "code": "POINT_001",
            "address": "D100",
            "type": 3,
            "num": 1,
            "coefficient": 1.0,
            "precision": 2,
        },
        {
            "code": "POINT_002",
            "address": "D101",
            "type": 3,
            "num": 1,
            "coefficient": 0.1,
            "precision": 1,
        },
        {
            "code": "POINT_003",
            "address": "D102",
            "type": 3,
            "num": 1,
            "coefficient": 1.0,
            "precision": 0,
        },
    ]


@pytest.fixture
def sample_storage_config():
    """Sample storage configuration for testing."""
    return {
        "host": "localhost",
        "port": 8086,
        "token": "test-token",
        "org": "test-org",
        "bucket": "test-bucket",
    }
