"""Executable REST inventory and cross-framework response conventions."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.urls import resolve
from rest_framework.test import APIClient

from configuration import models

from .helpers import _http_methods, current_business_routes, load_contract


pytestmark = pytest.mark.django_db


def test_canonical_route_inventory_matches_django() -> None:
    contract = load_contract("rest-v1.json")
    routes = current_business_routes()
    assert len(routes) == contract["canonical_route_count"]
    assert routes == contract["routes"]


def test_platform_route_inventory_matches_django() -> None:
    for item in load_contract("rest-v1.json")["platform_routes"]:
        callback = resolve(item["path"]).func
        assert _http_methods(callback) == item["methods"]


def test_limit_offset_and_drf_error_envelopes() -> None:
    contract = load_contract("rest-v1.json")
    client = APIClient()
    for index in range(3):
        models.Site.objects.create(code=f"contract-site-{index}", name=f"Site {index}")

    page = client.get("/api/config/sites/?limit=1&offset=1")
    assert page.status_code == 200
    assert list(page.data) == contract["pagination"]["response_keys"]
    assert page.data["count"] == 3
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
