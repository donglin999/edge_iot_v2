"""Executable REST inventory and cross-framework response conventions."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from acquisition import models as acquisition_models
from configuration import models

from .helpers import current_business_routes, current_platform_routes, load_contract


pytestmark = pytest.mark.django_db


def test_canonical_route_inventory_matches_django() -> None:
    contract = load_contract("rest-v1.json")
    routes = current_business_routes()
    assert len(routes) == contract["canonical_route_count"]
    assert routes == contract["routes"]


def test_platform_route_inventory_matches_django() -> None:
    contract = load_contract("rest-v1.json")
    assert current_platform_routes() == contract["platform_routes"]


def test_limit_offset_and_drf_error_envelopes() -> None:
    contract = load_contract("rest-v1.json")
    client = APIClient()
    models.Site.objects.bulk_create([
        models.Site(code=f"contract-site-{index:04d}", name=f"Site {index}")
        for index in range(1005)
    ])

    default_page = client.get("/api/config/sites/")
    assert default_page.status_code == 200
    assert len(default_page.data["results"]) == contract["pagination"]["default_limit"]
    assert type(default_page.data["count"]) is int
    assert isinstance(default_page.data["next"], str)
    assert default_page.data["previous"] is None
    assert isinstance(default_page.data["results"], list)

    maximum_page = client.get("/api/config/sites/?limit=999999")
    assert maximum_page.status_code == 200
    assert len(maximum_page.data["results"]) == contract["pagination"]["maximum_limit"]

    page = client.get("/api/config/sites/?limit=1&offset=1")
    assert page.status_code == 200
    assert list(page.data) == contract["pagination"]["response_keys"]
    assert page.data["count"] == 1005
    assert len(page.data["results"]) == 1

    invalid = client.post("/api/config/sites/", {"code": "", "name": ""}, format="json")
    assert invalid.status_code == 400
    assert sorted(invalid.data) == contract["error_families"]["drf_validation"]["top_level_keys"]
    assert all(isinstance(messages, list) for messages in invalid.data.values())

    missing = client.get("/api/config/sites/999999/")
    assert missing.status_code == 404
    assert list(missing.data) == contract["error_families"]["drf_not_found"]["top_level_keys"]
    assert isinstance(missing.data["detail"], str)


def test_generic_model_lifecycle_statuses() -> None:
    statuses = load_contract("rest-v1.json")["generic_statuses"]
    client = APIClient()

    created = client.post(
        "/api/config/sites/",
        {"code": "lifecycle-site", "name": "Lifecycle"},
        format="json",
    )
    assert created.status_code == statuses["create"]
    site_id = created.data["id"]
    assert client.get(f"/api/config/sites/{site_id}/").status_code == statuses["retrieve"]
    assert client.put(
        f"/api/config/sites/{site_id}/",
        {"code": "lifecycle-site", "name": "Replaced", "description": ""},
        format="json",
    ).status_code == statuses["update"]
    assert client.patch(
        f"/api/config/sites/{site_id}/",
        {"name": "Patched"},
        format="json",
    ).status_code == statuses["partial_update"]
    assert client.delete(f"/api/config/sites/{site_id}/").status_code == statuses["destroy"]


def test_decimal_fields_are_fixed_precision_json_strings() -> None:
    contract = load_contract("rest-v1.json")
    site = models.Site.objects.create(code="decimal-site", name="Decimal Site")
    device = models.Device.objects.create(
        site=site,
        code="decimal-device",
        name="Decimal Device",
        protocol="modbus_tcp",
    )
    channel = models.Channel.objects.create(
        device=device,
        name="channel",
        number=1,
        sampling_rate_hz=Decimal("1.25"),
    )
    template = models.PointTemplate.objects.create(
        name="温度",
        english_name="temperature",
        coefficient=Decimal("1.2345"),
    )
    point = models.Point.objects.create(
        device=device,
        template=template,
        code="temperature",
        address="40001",
        sample_rate_hz=Decimal("2.50"),
    )
    task = models.AcqTask.objects.create(
        code="decimal-task",
        name="Decimal Task",
        sample_rate_hz=Decimal("3.75"),
    )
    task.points.add(point)

    client = APIClient()
    responses = {
        "channel.sampling_rate_hz": client.get(f"/api/config/channels/{channel.id}/").json()["sampling_rate_hz"],
        "point.sample_rate_hz": client.get(f"/api/config/points/{point.id}/").json()["sample_rate_hz"],
        "point.template_detail.coefficient": client.get(f"/api/config/points/{point.id}/").json()["template_detail"]["coefficient"],
        "task.sample_rate_hz": client.get(f"/api/config/tasks/{task.id}/").json()["sample_rate_hz"],
    }
    assert responses == contract["decimal_string_examples"]
    assert all(isinstance(value, str) for value in responses.values())


def test_excel_error_families_remain_distinct() -> None:
    contract = load_contract("rest-v1.json")
    client = APIClient()

    v2 = client.post("/api/config/protocol-excel/import/", {}, format="multipart")
    assert v2.status_code == 400
    assert sorted(v2.data) == contract["error_families"]["excel_v2"]["top_level_keys"]
    assert sorted(v2.data["errors"][0]) == contract["error_families"]["excel_v2"]["item_keys"]

    scada = client.post("/api/config/scada-gateways/import/", {}, format="multipart")
    assert scada.status_code == 400
    assert sorted(scada.data) == contract["error_families"]["excel_scada"]["top_level_keys"]
    assert sorted(scada.data["errors"][0]) == contract["error_families"]["excel_scada"]["item_keys"]


def test_point_history_upstream_failure_is_legacy_http_200() -> None:
    family = load_contract("rest-v1.json")["error_families"]["point_history_upstream_failure"]
    client = APIClient()

    with patch("storage.StorageRegistry.create", side_effect=RuntimeError("synthetic outage")):
        response = client.get(
            "/api/acquisition/sessions/point-history/",
            {"point_code": "synthetic-point", "start_time": "-1h"},
        )

    assert response.status_code == family["status"]
    assert sorted(response.data) == family["top_level_keys"]
    assert response.data["count"] == 0
    assert response.data["data"] == []
    assert response.data["error"] == "synthetic outage"


def _assert_json_scalars(actual: dict, expected: dict) -> None:
    assert actual == expected
    for key, expected_value in expected.items():
        assert type(actual[key]) is type(expected_value), key


def test_json_and_influx_scalar_wire_types_match_examples() -> None:
    examples = load_contract("rest-v1.json")["json_value_rules"]["examples"]
    client = APIClient()
    site = models.Site.objects.create(code="scalar-site", name="Scalar Site")
    device = models.Device.objects.create(
        site=site,
        code="scalar-device",
        name="Scalar Device",
        protocol="modbus_tcp",
    )
    task = models.AcqTask.objects.create(code="scalar-task", name="Scalar Task")
    session = acquisition_models.AcquisitionSession.objects.create(task=task)
    now = timezone.now()

    acquisition_models.DataPoint.objects.bulk_create([
        acquisition_models.DataPoint(
            session=session,
            point_code=key,
            timestamp=now,
            value=value,
        )
        for key, value in examples["data_point_value"].items()
    ])
    assert examples["data_point_null_accepted"] is False
    with pytest.raises(IntegrityError), transaction.atomic():
        acquisition_models.DataPoint.objects.create(
            session=session,
            point_code="null",
            timestamp=now,
            value=None,
        )
    stored = client.get(f"/api/acquisition/sessions/{session.id}/data-points/")
    assert stored.status_code == 200
    _assert_json_scalars(
        {item["point_code"]: item["value"] for item in stored.data["results"]},
        examples["data_point_value"],
    )

    for key in examples["influx_latest_value"]:
        models.Point.objects.create(
            device=device,
            code=key,
            address=key,
        )
    raw_latest = {
        **examples["influx_latest_value"],
        "numeric_string": "3.25",
    }
    latest_storage = MagicMock()
    latest_storage.query.return_value = [
        {"_field": key, "_value": value, "_time": None, "quality": "good"}
        for key, value in raw_latest.items()
    ]
    with patch("storage.StorageRegistry.create", return_value=latest_storage):
        latest = client.get(f"/api/config/devices/{device.id}/latest-values/")
    assert latest.status_code == 200
    _assert_json_scalars(
        {item["point_code"]: item["value"] for item in latest.data["points"]},
        examples["influx_latest_value"],
    )

    raw_history = {
        **examples["influx_history_value"],
        "numeric_string": "3.25",
    }
    history_storage = MagicMock()
    history_storage.query.return_value = [
        {
            "_time": "2026-01-01T00:00:00Z",
            "_value": value,
            "quality": key,
        }
        for key, value in raw_history.items()
    ] + [{
        "_time": "2026-01-01T00:00:00Z",
        "_value": None,
        "quality": "null",
    }]
    with patch("storage.StorageRegistry.create", return_value=history_storage):
        history = client.get(
            "/api/acquisition/sessions/point-history/",
            {"point_code": "scalar-point"},
        )
    assert history.status_code == 200
    _assert_json_scalars(
        {item["quality"]: item["value"] for item in history.data["data"]},
        examples["influx_history_value"],
    )
    assert "null" not in {item["quality"] for item in history.data["data"]}


def test_tasks_ignore_site_code_and_custom_collection_shapes() -> None:
    contract = load_contract("rest-v1.json")["legacy_endpoint_behaviors"]
    client = APIClient()
    tasks = []
    for suffix in ("a", "b"):
        site = models.Site.objects.create(code=f"shape-site-{suffix}", name=suffix)
        device = models.Device.objects.create(
            site=site,
            code=f"shape-device-{suffix}",
            name=suffix,
            protocol="modbus_tcp",
        )
        point = models.Point.objects.create(
            device=device,
            code=f"shape-point-{suffix}",
            address="1",
        )
        task = models.AcqTask.objects.create(code=f"shape-task-{suffix}", name=suffix)
        task.points.add(point)
        tasks.append(task)

    ignored = contract["ignored_query_parameters"][0]
    response = client.get(ignored["path"], {ignored["parameter"]: "shape-site-a"})
    assert response.status_code == 200
    assert {item["code"] for item in response.data["results"]} == {
        "shape-task-a",
        "shape-task-b",
    }

    session = acquisition_models.AcquisitionSession.objects.create(task=tasks[0])
    acquisition_models.DataPoint.objects.create(
        session=session,
        point_code="shape-point-a",
        timestamp=timezone.now(),
        value=1,
    )
    responses = {
        "/api/config/tasks/{id}/points/": client.get(
            f"/api/config/tasks/{tasks[0].id}/points/"
        ),
        "/api/config/tasks/overview/": client.get(
            "/api/config/tasks/overview/", {"site_code": "shape-site-a"}
        ),
        "/api/acquisition/sessions/active/": client.get(
            "/api/acquisition/sessions/active/"
        ),
        "/api/acquisition/sessions/{id}/data-points/": client.get(
            f"/api/acquisition/sessions/{session.id}/data-points/"
        ),
    }
    for expected in contract["custom_collection_actions"]:
        actual = responses[expected["path"]]
        assert actual.status_code == expected["status"]
        expected_type = list if expected["top_level_type"] == "array" else dict
        assert isinstance(actual.data, expected_type)
        if "top_level_keys" in expected:
            assert sorted(actual.data) == expected["top_level_keys"]


def test_malformed_numeric_query_is_legacy_http_500() -> None:
    expected = load_contract("rest-v1.json")["legacy_endpoint_behaviors"][
        "malformed_numeric_query"
    ]
    client = APIClient()
    client.raise_request_exception = False
    response = client.get(
        expected["path"],
        {"point_code": "synthetic-point", expected["parameter"]: expected["example"]},
    )
    assert response.status_code == expected["status"]


def test_connection_failure_is_legacy_http_200_success_false() -> None:
    expected = load_contract("rest-v1.json")["legacy_endpoint_behaviors"][
        "device_test_connection_failure"
    ]
    site = models.Site.objects.create(code="connection-site", name="Connection")
    device = models.Device.objects.create(
        site=site,
        code="connection-device",
        name="Connection Device",
        protocol="modbus_tcp",
        metadata={"source_ip": "192.0.2.1", "source_port": 502},
    )
    async_result = MagicMock(id="synthetic-connection-job")
    async_result.get.side_effect = RuntimeError("synthetic connection failure")

    with patch(
        "acquisition.tasks.trace_protocol_connection.apply_async",
        return_value=async_result,
    ):
        response = APIClient().post(
            expected["path"].replace("{id}", str(device.id)),
            {},
            format="json",
        )

    assert response.status_code == expected["status"]
    assert response.data["success"] is expected["success"]
    assert sorted(response.data) == expected["top_level_keys"]


def test_two_control_surfaces_keep_distinct_start_stop_envelopes() -> None:
    actions = load_contract("rest-v1.json")["legacy_endpoint_behaviors"][
        "control_actions"
    ]
    client = APIClient()
    site = models.Site.objects.create(code="control-site", name="Control")
    device = models.Device.objects.create(
        site=site,
        code="control-device",
        name="Control Device",
        protocol="modbus_tcp",
        metadata={"source_ip": "192.0.2.2", "source_port": 502},
    )
    point = models.Point.objects.create(
        device=device,
        code="control-point",
        address="1",
    )
    task = models.AcqTask.objects.create(code="control-task", name="Control Task")
    task.points.add(point)

    config_start_spec = actions["config_task_start"]
    with patch(
        "acquisition.tasks.start_acquisition_task.delay",
        return_value=SimpleNamespace(id="synthetic-config-start"),
    ):
        config_start = client.post(
            config_start_spec["path"].replace("{id}", str(task.id)),
            {},
            format="json",
        )
    assert config_start.status_code == config_start_spec["status"]
    assert sorted(config_start.data) == config_start_spec["top_level_keys"]

    protocol = MagicMock()
    protocol.read_points.return_value = [{"code": point.code, "value": 1}]
    acquisition_start_spec = actions["acquisition_start_task"]
    with (
        patch("acquisition.views.ProtocolRegistry.create", return_value=protocol),
        patch(
            "acquisition.views.tasks.start_acquisition_task.delay",
            return_value=SimpleNamespace(id="synthetic-acquisition-start"),
        ),
        patch("acquisition.views.time.sleep"),
    ):
        acquisition_start = client.post(
            acquisition_start_spec["path"],
            {"task_id": task.id},
            format="json",
        )
    assert acquisition_start.status_code == acquisition_start_spec["status"]
    assert sorted(acquisition_start.data) == acquisition_start_spec["top_level_keys"]
    assert sorted(acquisition_start.data["validation"]) == acquisition_start_spec[
        "validation_keys"
    ]

    session = acquisition_models.AcquisitionSession.objects.create(
        task=task,
        status=acquisition_models.AcquisitionSession.STATUS_RUNNING,
        celery_task_id="",
    )
    config_stop_spec = actions["config_task_stop"]
    with patch("acquisition.tasks.stop_acquisition_task.delay"):
        config_stop = client.post(
            config_stop_spec["path"].replace("{id}", str(task.id)),
            {},
            format="json",
        )
    assert config_stop.status_code == config_stop_spec["status"]
    assert sorted(config_stop.data) == config_stop_spec["top_level_keys"]

    acquisition_stop_spec = actions["acquisition_session_stop"]
    acquisition_stop = client.post(
        acquisition_stop_spec["path"].replace("{id}", str(session.id)),
        {"reason": "synthetic contract stop"},
        format="json",
    )
    assert acquisition_stop.status_code == acquisition_stop_spec["status"]
    assert sorted(acquisition_stop.data) == acquisition_stop_spec["top_level_keys"]
