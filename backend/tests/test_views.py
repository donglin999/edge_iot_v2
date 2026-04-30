"""Unit tests for API views."""
import pytest
from unittest.mock import patch, MagicMock
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient

from acquisition import models as acq_models
from configuration import models as config_models
from tests.fixtures.factories import *


@pytest.fixture
def api_client():
    """API test client."""
    return APIClient()


@pytest.mark.django_db
class TestDeviceViewSet:
    """Tests for Device API endpoints."""

    def test_list_devices(self, api_client, create_device, create_site):
        """Test listing devices."""
        site = create_site()
        device1 = create_device(site=site, code="DEV_001", ip="192.168.1.101")
        device2 = create_device(site=site, code="DEV_002", ip="192.168.1.102")

        response = api_client.get("/api/config/devices/")

        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) >= 2

    def test_create_device(self, api_client, create_site):
        """Test creating a device."""
        site = create_site()

        data = {
            "site": site.id,
            "code": "NEW_DEV",
            "name": "New Device",
            "protocol": "modbus_tcp",
            "ip_address": "192.168.1.200",
            "port": 502
        }

        response = api_client.post("/api/config/devices/", data, format="json")

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["code"] == "NEW_DEV"

    def test_retrieve_device(self, api_client, create_device):
        """Test retrieving a single device."""
        device = create_device(code="RETRIEVE_TEST")

        response = api_client.get(f"/api/config/devices/{device.id}/")

        assert response.status_code == status.HTTP_200_OK
        assert response.data["code"] == "RETRIEVE_TEST"

    def test_update_device(self, api_client, create_device):
        """Test updating a device."""
        device = create_device(name="Original Name")

        data = {"name": "Updated Name"}
        response = api_client.patch(f"/api/config/devices/{device.id}/", data, format="json")

        assert response.status_code == status.HTTP_200_OK
        assert response.data["name"] == "Updated Name"

    def test_delete_device(self, api_client, create_device):
        """Test deleting a device."""
        device = create_device()

        response = api_client.delete(f"/api/config/devices/{device.id}/")

        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert not config_models.Device.objects.filter(id=device.id).exists()

    def test_list_points_action(self, api_client, create_device, create_point):
        """Test listing points for a device."""
        device = create_device()
        point1 = create_point(device=device, code="P1")
        point2 = create_point(device=device, code="P2")

        response = api_client.get(f"/api/config/devices/{device.id}/points/")

        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 2


@pytest.mark.django_db
class TestAcqTaskViewSet:
    """Tests for AcqTask API endpoints."""

    def test_list_tasks(self, api_client, create_task):
        """Test listing tasks."""
        task1 = create_task(code="TASK_001")
        task2 = create_task(code="TASK_002")

        response = api_client.get("/api/config/tasks/")

        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) >= 2

    def test_create_task(self, api_client, create_point):
        """Test creating a task."""
        point = create_point()

        data = {
            "code": "NEW_TASK",
            "name": "New Task",
            "points": [point.id]
        }

        response = api_client.post("/api/config/tasks/", data, format="json")

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["code"] == "NEW_TASK"

    def test_retrieve_task(self, api_client, create_task):
        """Test retrieving a single task."""
        task = create_task(code="RETRIEVE_TASK")

        response = api_client.get(f"/api/config/tasks/{task.id}/")

        assert response.status_code == status.HTTP_200_OK
        assert response.data["code"] == "RETRIEVE_TASK"

    def test_task_points_action(self, api_client, create_task, create_point):
        """Test listing points for a task."""
        point1 = create_point(code="P1")
        point2 = create_point(code="P2")
        task = create_task(points=[point1, point2])

        response = api_client.get(f"/api/config/tasks/{task.id}/points/")

        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 2

    @patch("acquisition.tasks.start_acquisition_task.delay")
    def test_start_task_action(self, mock_delay, api_client, create_task, create_session):
        """Test starting a task."""
        mock_result = MagicMock()
        mock_result.id = "test-celery-task-id"
        mock_result.get.return_value = {"status": "started"}
        mock_delay.return_value = mock_result

        task = create_task()
        # Create a stopped session so start can proceed
        session = create_session(task=task, status="stopped")

        data = {"worker": "test-worker", "note": "test start"}
        response = api_client.post("/api/config/tasks/{}/start/".format(task.id), data, format="json")

        # Should return success (session created)
        assert response.status_code == status.HTTP_200_OK

    def test_stop_task_action(self, api_client, create_task, create_session):
        """Test stopping a task."""
        task = create_task()
        session = create_session(task=task, status="running")

        with patch("acquisition.views.tasks.stop_acquisition_task.delay") as mock_stop:
            response = api_client.post(f"/api/config/tasks/{task.id}/stop/", format="json")

            # Should return success
            assert response.status_code == status.HTTP_200_OK


@pytest.mark.django_db
class TestAcquisitionSessionViewSet:
    """Tests for AcquisitionSession API endpoints."""

    def test_list_sessions(self, api_client, create_session):
        """Test listing sessions."""
        session = create_session()

        response = api_client.get("/api/acquisition/sessions/")

        assert response.status_code == status.HTTP_200_OK

    def test_retrieve_session(self, api_client, create_session):
        """Test retrieving a session."""
        session = create_session()

        response = api_client.get(f"/api/acquisition/sessions/{session.id}/")

        assert response.status_code == status.HTTP_200_OK

    def test_active_sessions(self, api_client, create_session):
        """Test listing active sessions."""
        session1 = create_session(status="running")
        session2 = create_session(status="stopped")

        response = api_client.get("/api/acquisition/sessions/active/")

        assert response.status_code == status.HTTP_200_OK
        # Only running sessions should be returned
        for item in response.data:
            assert item["status"] == "running"

    def test_stop_session(self, api_client, create_session):
        """Test stopping a session."""
        session = create_session(status="running")

        with patch("acquisition.views.tasks.stop_acquisition_task.delay") as mock_stop:
            response = api_client.post(f"/api/acquisition/sessions/{session.id}/stop/")

            assert response.status_code == status.HTTP_200_OK

    @patch("subprocess.run")
    def test_point_history(self, mock_subprocess, api_client, create_session):
        """Test querying point history."""
        session = create_session()

        # Mock subprocess for docker exec
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="[]", stderr="")

        response = api_client.get(
            "/api/acquisition/sessions/point-history/",
            {"point_code": "TEST_POINT", "start_time": "-1h"}
        )

        assert response.status_code == status.HTTP_200_OK


@pytest.mark.django_db
class TestPointViewSet:
    """Tests for Point API endpoints."""

    def test_list_points(self, api_client, create_point):
        """Test listing points."""
        point = create_point()

        response = api_client.get("/api/config/points/")

        assert response.status_code == status.HTTP_200_OK

    def test_create_point(self, api_client, create_device, create_point_template):
        """Test creating a point."""
        device = create_device()
        template = create_point_template()

        data = {
            "device": device.id,
            "template": template.id,
            "code": "NEW_POINT",
            "address": "D200"
        }

        response = api_client.post("/api/config/points/", data, format="json")

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["code"] == "NEW_POINT"

    def test_retrieve_point(self, api_client, create_point):
        """Test retrieving a point."""
        point = create_point(code="RETRIEVE_P")

        response = api_client.get(f"/api/config/points/{point.id}/")

        assert response.status_code == status.HTTP_200_OK
        assert response.data["code"] == "RETRIEVE_P"


@pytest.mark.django_db
class TestSiteViewSet:
    """Tests for Site API endpoints."""

    def test_list_sites(self, api_client, create_site):
        """Test listing sites."""
        site = create_site()

        response = api_client.get("/api/config/sites/")

        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) >= 1

    def test_create_site(self, api_client):
        """Test creating a site."""
        data = {
            "code": "NEW_SITE",
            "name": "New Site",
            "description": "Test description"
        }

        response = api_client.post("/api/config/sites/", data, format="json")

        assert response.status_code == status.HTTP_201_CREATED
        assert response.data["code"] == "NEW_SITE"

    def test_retrieve_site(self, api_client, create_site):
        """Test retrieving a site."""
        site = create_site(code="RETRIEVE_S")

        response = api_client.get(f"/api/config/sites/{site.id}/")

        assert response.status_code == status.HTTP_200_OK
        assert response.data["code"] == "RETRIEVE_S"


@pytest.mark.django_db
class TestConnectionTestViewSet:
    """Tests for ConnectionTest API endpoints."""

    @patch("acquisition.tasks.ProtocolRegistry")
    def test_create_connection_test(self, mock_registry, api_client, create_device):
        """Test creating a connection test."""
        mock_protocol = MagicMock()
        mock_protocol.connect.return_value = True
        mock_protocol.health_check.return_value = True
        mock_registry.create.return_value = mock_protocol

        device = create_device()

        data = {
            "protocol_type": "mock_modbus",
            "device_config": {
                "source_ip": device.ip_address,
                "source_port": device.port,
            }
        }
        response = api_client.post("/api/acquisition/connection-tests/", data, format="json")

        assert response.status_code == status.HTTP_200_OK


@pytest.mark.django_db
class TestStorageTestViewSet:
    """Tests for StorageTest API endpoints."""

    @patch("acquisition.tasks.StorageRegistry")
    def test_create_storage_test(self, mock_registry, api_client):
        """Test creating a storage test."""
        mock_storage = MagicMock()
        mock_storage.connect.return_value = True
        mock_storage.health_check.return_value = True
        mock_registry.create.return_value = mock_storage

        data = {
            "storage_type": "influxdb",
            "storage_config": {
                "host": "localhost",
                "port": 8086,
                "token": "test",
                "org": "test",
                "bucket": "test"
            }
        }

        response = api_client.post("/api/acquisition/storage-tests/", data, format="json")

        assert response.status_code == status.HTTP_200_OK
