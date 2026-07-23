"""Regression tests for the configuration-app review batch (rank8/11/21).

rank8  — AcqTaskViewSet.stop / .rollback used to filter
         AcquisitionSession.objects.filter(status__in=[RUNNING, RUNNING])
         (copy/paste bug — PAUSED was never matched). That let a paused
         session dodge both the rollback guard and task-level stop.
rank11 — SCADA two-sheet Excel import's _to_int() choked on float cells
         (openpyxl commonly hands back "whole" numbers as floats, e.g.
         source_port=8883.0, mqtt_qos=0.0) and silently turned them into
         validation errors.
rank21 — AcqTaskSerializer.validate() did `{p.device.protocol for p in
         incoming_points}`, one query per point (N+1) for unselected FKs.
"""
from unittest.mock import patch

import pytest
from decimal import Decimal
from rest_framework import status
from rest_framework.test import APIClient

from acquisition import models as acq_models
from configuration import models as config_models
from configuration.serializers import AcqTaskSerializer
from configuration.services.scada_excel import _to_int
from tests.fixtures.factories import *

from tests.test_scada_excel import (
    GATEWAY_HEADERS,
    GATEWAY_ROW,
    build_workbook,
    upload,
)


@pytest.fixture
def api_client():
    return APIClient()


# ---------------------------------------------------------------------------
# rank8 — PAUSED sessions must not slip past stop()/rollback()'s RUNNING check
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestPausedSessionIsTreatedAsActive:

    def test_stop_task_converges_paused_session(self, api_client, create_task, create_session):
        """A paused (not just running) session must be found and stopped."""
        task = create_task()
        session = create_session(task=task, status=acq_models.AcquisitionSession.STATUS_PAUSED)

        with patch("acquisition.views.tasks.stop_acquisition_task.delay") as mock_stop:
            response = api_client.post(f"/api/config/tasks/{task.id}/stop/", format="json")

        assert response.status_code == status.HTTP_200_OK
        assert response.data["session_id"] == session.id
        mock_stop.assert_called_once_with(session.id)

    def test_start_task_rejects_when_session_paused(self, api_client, create_task, create_session):
        """start() must not spin up a second session while one is merely paused."""
        task = create_task()
        create_session(task=task, status=acq_models.AcquisitionSession.STATUS_PAUSED)

        response = api_client.post(f"/api/config/tasks/{task.id}/start/", {}, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "运行中" in response.data["detail"]

    def test_rollback_rejects_paused_session(self, api_client, create_task, create_session,
                                              create_config_version):
        """rollback() must refuse when the task has a paused session, not just a running one."""
        task = create_task()
        create_session(task=task, status=acq_models.AcquisitionSession.STATUS_PAUSED)
        version = create_config_version(task=task, payload={"foo": "bar"})

        response = api_client.post(f"/api/config/versions/{version.id}/rollback/", format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "无法回滚" in response.data["detail"]
        # No new version should have been created.
        assert task.versions.count() == 1

    def test_rollback_allowed_when_only_stopped_sessions(self, api_client, create_task,
                                                           create_session, create_config_version):
        """Sanity check: a stopped session must NOT block rollback (no over-blocking)."""
        task = create_task()
        create_session(task=task, status=acq_models.AcquisitionSession.STATUS_STOPPED)
        version = create_config_version(task=task, payload={"foo": "bar"})

        response = api_client.post(f"/api/config/versions/{version.id}/rollback/", format="json")

        assert response.status_code == status.HTTP_200_OK
        assert task.versions.count() == 2


# ---------------------------------------------------------------------------
# rank11 — SCADA _to_int must tolerate float-valued cells
# ---------------------------------------------------------------------------


class TestScadaExcelToIntToleratesFloats:

    @pytest.mark.parametrize("raw,expected", [
        (8883.0, 8883),
        (0.0, 0),
        ("8883.0", 8883),
        ("0.0", 0),
        (1, 1),
        ("1883", 1883),
        (1883.5, None),      # genuinely fractional -> reject
        ("not-a-number", None),
        (None, None),
        ("", None),
    ])
    def test_to_int(self, raw, expected):
        assert _to_int(raw) == expected

    def test_import_accepts_float_port_and_qos_cells(self, api_client, db):
        """End-to-end: a workbook produced by openpyxl-roundtrip (float cells
        for source_port / mqtt_qos) must import cleanly, not 400."""
        gateway_row = list(GATEWAY_ROW)
        port_idx = GATEWAY_HEADERS.index("source_port")
        qos_idx = GATEWAY_HEADERS.index("mqtt_qos")
        timeout_idx = GATEWAY_HEADERS.index("mqtt_read_timeout")
        gateway_row[port_idx] = 8883.0
        gateway_row[qos_idx] = 0.0
        gateway_row[timeout_idx] = 7.0

        wb_bytes = build_workbook(gateway_rows=[gateway_row])

        response = api_client.post(
            "/api/config/scada-gateways/import/",
            {"file": upload(wb_bytes)},
            format="multipart",
        )

        # 新建网关+设备时导入端点返回 201(创建),幂等重导才返回 200 —— 两者都算成功,
        # 关键是没被 400 挡住(即浮点单元格被 _to_int 正确解析)。
        assert response.status_code in (status.HTTP_200_OK, status.HTTP_201_CREATED), response.content
        gateway = config_models.ScadaGateway.objects.get(code=gateway_row[0])
        assert gateway.source_port == 8883
        assert gateway.mqtt_qos == 0


# ---------------------------------------------------------------------------
# rank21 — AcqTaskSerializer.validate must not N+1 over incoming points
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAcqTaskSerializerProtocolCheckIsNotNPlusOne:

    def _points(self, create_point, create_device, n):
        # 一个任务只能绑一台设备,所以全部测点放同一台设备上。
        device = create_device()
        return [create_point(device=device, code=f"p{i}") for i in range(n)]

    def test_validate_query_count_does_not_scale_with_point_count(
        self, create_point, create_device, django_assert_max_num_queries
    ):
        points = self._points(create_point, create_device, 12)
        attrs = {"sample_rate_hz": Decimal("10.0"), "points": points}

        serializer = AcqTaskSerializer()
        # 两个聚合查询(单设备校验 + 协议 cap 校验),都不随测点数增长 ——
        # 老代码是每个测点各查一次 device。
        with django_assert_max_num_queries(2):
            serializer.validate(attrs)

    def test_validate_still_enforces_protocol_rate_cap(
        self, create_device, create_point, create_site
    ):
        site = create_site()
        modbus_rtu_device = create_device(site=site, protocol="modbus_rtu")
        point = create_point(device=modbus_rtu_device)
        attrs = {"sample_rate_hz": Decimal("10.0"), "points": [point]}

        serializer = AcqTaskSerializer()
        with pytest.raises(Exception) as exc_info:
            serializer.validate(attrs)
        assert "modbus_rtu" in str(exc_info.value)

    def test_validate_allows_rate_within_protocol_cap(self, create_point):
        point = create_point()  # default device protocol: mock_modbus (cap 100Hz default)
        attrs = {"sample_rate_hz": Decimal("10.0"), "points": [point]}

        serializer = AcqTaskSerializer()
        result = serializer.validate(attrs)
        assert result == attrs
