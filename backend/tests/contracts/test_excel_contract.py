"""Workbook layout contract for v2, legacy and SCADA import/export."""
from __future__ import annotations

import ast
import io
import ipaddress
import json
import re
from dataclasses import fields
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from openpyxl import load_workbook
from rest_framework.test import APIClient

from acquisition.protocols import ProtocolRegistry
from acquisition.protocols.base import FieldSpec
from acquisition.services import templates as legacy_templates
from configuration.services import protocol_excel, scada_excel

from .helpers import (
    CONTRACT_ROOT,
    REPOSITORY_ROOT,
    clean_production_protocol_names,
    field_spec,
    load_contract,
)


def _header_values(sheet, row: int) -> list[str]:
    return [cell.value for cell in sheet[row] if cell.value is not None]


def _actual_header_rows(sheet, production_keys: list[str]) -> int:
    """Locate the parser key row in a real generated workbook."""

    matches = [
        row
        for row in range(1, min(sheet.max_row, 5) + 1)
        if _header_values(sheet, row) == production_keys
    ]
    assert len(matches) == 1
    return matches[0]


def _clean_protocol_sets() -> tuple[list[str], list[str]]:
    all_protocols = clean_production_protocol_names()
    v2_protocols = [
        name for name in all_protocols
        if name not in protocol_excel.EXCLUDED_PROTOCOLS
    ]
    return all_protocols, v2_protocols


def _synthetic_v2_blob(protocol: str) -> bytes:
    """Replace generated example identifiers before parsing or API import."""

    workbook = load_workbook(io.BytesIO(protocol_excel.build_template(protocol)))
    device_sheet = workbook[protocol_excel.SHEET_DEVICES]
    point_sheet = workbook[protocol_excel.SHEET_POINTS]
    device_columns = {
        cell.value: cell.column for cell in device_sheet[2] if cell.value is not None
    }
    point_columns = {
        cell.value: cell.column for cell in point_sheet[2] if cell.value is not None
    }
    device_values = {
        "device_name": "synthetic-device-001",
        "device_code": "synthetic-device-code-001",
        "site_code": "synthetic-site-001",
        "source_ip": "192.0.2.10",
        "endpoint_url": "opc.tcp://192.0.2.20:4840",
        "mqtt_username": "synthetic-user",
        "opcua_username": "synthetic-user",
    }
    for key, value in device_values.items():
        if key in device_columns:
            device_sheet.cell(3, device_columns[key], value)
    for row in range(3, point_sheet.max_row + 1):
        point_values = {
            "device_name": "synthetic-device-001",
            "code": f"synthetic-point-{row - 2:03d}",
            "description": "synthetic-description",
            "address": (
                "ns=2;s=SyntheticDevice.SyntheticTag"
                if protocol == "opcua"
                else None
            ),
        }
        for key, value in point_values.items():
            if key in point_columns and value is not None:
                point_sheet.cell(row, point_columns[key], value)

    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def test_v2_field_specs_and_real_template_layout_match_snapshot() -> None:
    root_contract = load_contract("excel-v1.json")
    contract = root_contract["v2"]
    field_names = [item.name for item in fields(FieldSpec)]
    registry_before = (dict(ProtocolRegistry._protocols), dict(ProtocolRegistry._aliases))
    _all_protocols, actual_protocols = _clean_protocol_sets()
    assert sorted(protocol_excel.EXCLUDED_PROTOCOLS) == contract["excluded_protocols"]
    assert actual_protocols == contract["protocols"]

    for name in actual_protocols:
        klass = ProtocolRegistry.get(name)
        expected = contract["schemas"][name]
        device_specs = protocol_excel._device_columns(klass)
        point_specs = protocol_excel._point_columns(klass)
        assert [field_spec(item) for item in device_specs] == expected["device"]
        assert [field_spec(item) for item in point_specs] == expected["point"]
        assert all(list(item) == field_names for item in expected["device"])
        assert all(list(item) == field_names for item in expected["point"])

        workbook = load_workbook(io.BytesIO(protocol_excel.build_template(name)))
        assert workbook.sheetnames == contract["sheets"]
        expected_device_labels = [
            f"*{spec['label']}" if spec["required"] else spec["label"]
            for spec in expected["device"]
        ]
        expected_point_labels = [
            f"*{spec['label']}" if spec["required"] else spec["label"]
            for spec in expected["point"]
        ]
        assert _header_values(workbook["设备"], 1) == expected_device_labels
        assert _header_values(workbook["测点"], 1) == expected_point_labels
        assert _actual_header_rows(
            workbook["设备"], [item.name for item in device_specs]
        ) == contract["header_rows"]
        assert _actual_header_rows(
            workbook["测点"], [item.name for item in point_specs]
        ) == contract["header_rows"]
        workbook.close()

    assert (dict(ProtocolRegistry._protocols), dict(ProtocolRegistry._aliases)) == registry_before


def test_legacy_40_column_layout_matches_snapshot() -> None:
    root_contract = load_contract("excel-v1.json")
    contract = root_contract["legacy"]
    registry_before = (dict(ProtocolRegistry._protocols), dict(ProtocolRegistry._aliases))
    protocols, _v2_protocols = _clean_protocol_sets()
    specs = legacy_templates._column_specs(protocols)
    assert [field_spec(item) for item in specs] == contract["field_specs"]
    assert all(
        list(item) == [field.name for field in fields(FieldSpec)]
        for item in contract["field_specs"]
    )

    workbook = load_workbook(io.BytesIO(legacy_templates.build_template(protocols)))
    assert workbook.sheetnames == contract["sheets"]
    expected_header = contract["fixed_columns"] + [item.name for item in specs]
    assert _actual_header_rows(
        workbook["采集点配置"], expected_header
    ) == contract["header_rows"]
    workbook.close()
    assert (dict(ProtocolRegistry._protocols), dict(ProtocolRegistry._aliases)) == registry_before


def test_scada_single_header_layout_matches_snapshot() -> None:
    contract = load_contract("excel-v1.json")["scada"]
    workbook = load_workbook(io.BytesIO(scada_excel.build_scada_template()))
    assert workbook.sheetnames == contract["sheets"]
    gateway_keys = [
        spec.name for spec in (*scada_excel.GATEWAY_COLUMNS, *scada_excel.TASK_COLUMNS)
    ]
    point_keys = [spec.name for spec in scada_excel.POINT_COLUMNS]
    assert gateway_keys == contract["gateway_columns"]
    assert point_keys == contract["point_columns"]
    assert _actual_header_rows(
        workbook["网关服务"], gateway_keys
    ) == contract["header_rows"]
    assert _actual_header_rows(
        workbook["设备与测点"], point_keys
    ) == contract["header_rows"]
    workbook.close()


def test_v2_templates_parse_in_memory_and_integer_cells_stay_integers() -> None:
    root_contract = load_contract("excel-v1.json")
    _all_protocols, protocols = _clean_protocol_sets()
    assert protocols == root_contract["v2"]["protocols"]
    for name in protocols:
        blob = _synthetic_v2_blob(name)
        parsed = protocol_excel.parse_workbook(io.BytesIO(blob))
        assert parsed.is_valid, [error.to_dict() for error in parsed.errors]
        assert parsed.protocol == name
        assert parsed.devices
        assert parsed.devices[0].device_name == "synthetic-device-001"
        assert parsed.devices[0].points

    blob = _synthetic_v2_blob("modbus_tcp")
    workbook = load_workbook(io.BytesIO(blob), data_only=True)
    device_sheet = workbook["设备"]
    point_sheet = workbook["测点"]
    device_columns = {
        cell.value: cell.column for cell in device_sheet[2] if cell.value is not None
    }
    point_columns = {
        cell.value: cell.column for cell in point_sheet[2] if cell.value is not None
    }
    assert type(device_sheet.cell(3, device_columns["source_port"]).value) is int
    assert type(device_sheet.cell(3, device_columns["slave_id"]).value) is int
    assert type(point_sheet.cell(3, point_columns["function_code"]).value) is int
    assert type(point_sheet.cell(3, point_columns["num"]).value) is int
    workbook.close()

    parsed = protocol_excel.parse_workbook(io.BytesIO(blob))
    assert type(parsed.devices[0].config["source_port"]) is int
    assert type(parsed.devices[0].config["slave_id"]) is int
    assert type(parsed.devices[0].points[0]["extra"]["function_code"]) is int
    assert type(parsed.devices[0].points[0]["extra"]["num"]) is int


@pytest.mark.django_db
def test_download_mime_and_multipart_upload_field_match_contract() -> None:
    contract = load_contract("excel-v1.json")
    client = APIClient()
    for path in (
        "/api/config/protocol-excel/template/?protocol=modbus_tcp",
        "/api/config/scada-gateways/template/",
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert response["Content-Type"] == contract["download_mime"]

    blob = _synthetic_v2_blob("modbus_tcp")
    wrong = client.post(
        "/api/config/protocol-excel/import/",
        {
            "upload": SimpleUploadedFile(
                "synthetic-contract.xlsx",
                blob,
                content_type=contract["download_mime"],
            )
        },
        format="multipart",
    )
    assert wrong.status_code == 400
    assert wrong.data["errors"][0]["column"] == contract["upload_field"]

    correct = client.post(
        "/api/config/protocol-excel/import/",
        {
            contract["upload_field"]: SimpleUploadedFile(
                "synthetic-contract.xlsx",
                blob,
                content_type=contract["download_mime"],
            )
        },
        format="multipart",
    )
    assert correct.status_code == 201
    assert correct.data["errors"] == []


def test_synthetic_credentials_round_trip_only_in_memory() -> None:
    contract = load_contract("excel-v1.json")
    secret_specs = [
        spec
        for schema in contract["v2"]["schemas"].values()
        for section in ("device", "point")
        for spec in schema[section]
        if spec["kind"] == "secret"
    ] + [
        spec for spec in contract["legacy"]["field_specs"]
        if spec["kind"] == "secret"
    ]
    assert secret_specs
    assert all(spec["default"] in (None, "") for spec in secret_specs)
    assert all(
        spec["example"] is None or str(spec["example"]).startswith("<")
        for spec in secret_specs
    )

    synthetic_secret = "SYNTHETIC_CONTRACT_VALUE_NOT_A_CREDENTIAL"

    mqtt_blob = _synthetic_v2_blob("mqtt")
    mqtt_workbook = load_workbook(io.BytesIO(mqtt_blob))
    mqtt_sheet = mqtt_workbook["设备"]
    mqtt_columns = {
        cell.value: cell.column for cell in mqtt_sheet[2] if cell.value is not None
    }
    mqtt_sheet.cell(3, mqtt_columns["mqtt_username"], "synthetic-user.invalid")
    mqtt_sheet.cell(3, mqtt_columns["mqtt_password"], synthetic_secret)
    mqtt_buffer = io.BytesIO()
    mqtt_workbook.save(mqtt_buffer)
    mqtt_workbook.close()
    mqtt_buffer.seek(0)
    mqtt_parsed = protocol_excel.parse_workbook(mqtt_buffer)
    assert mqtt_parsed.is_valid
    assert mqtt_parsed.devices[0].config["mqtt_username"] == "synthetic-user.invalid"
    assert mqtt_parsed.devices[0].config["mqtt_password"] == synthetic_secret

    scada_workbook = load_workbook(io.BytesIO(scada_excel.build_scada_template()))
    gateway_sheet = scada_workbook["网关服务"]
    point_sheet = scada_workbook["设备与测点"]
    gateway_columns = {
        cell.value: cell.column for cell in gateway_sheet[1] if cell.value is not None
    }
    point_columns = {
        cell.value: cell.column for cell in point_sheet[1] if cell.value is not None
    }
    assert type(gateway_sheet.cell(2, gateway_columns["source_port"]).value) is int
    assert type(gateway_sheet.cell(2, gateway_columns["mqtt_qos"]).value) is int
    synthetic_gateway_values = {
        "code": "synthetic-gateway-001",
        "name": "Synthetic Gateway",
        "source_ip": "192.0.2.30",
        "mqtt_username": "synthetic-user.invalid",
        "mqtt_client_id": "synthetic-client-001",
        "product_key": "synthetic-product-key",
        "task_code": "synthetic-task-001",
        "task_name": "Synthetic Task",
    }
    for key, value in synthetic_gateway_values.items():
        gateway_sheet.cell(2, gateway_columns[key], value)
    gateway_sheet.cell(2, gateway_columns["mqtt_password"], synthetic_secret)
    for row in range(2, point_sheet.max_row + 1):
        point_sheet.cell(row, point_columns["device_name"], "synthetic-device-001")
        point_sheet.cell(row, point_columns["device_label"], "Synthetic Device")
        point_sheet.cell(row, point_columns["code"], f"synthetic-point-{row - 1:03d}")
        point_sheet.cell(row, point_columns["description"], "synthetic-description")
    scada_buffer = io.BytesIO()
    scada_workbook.save(scada_buffer)
    scada_workbook.close()
    scada_buffer.seek(0)
    scada_parsed = scada_excel.parse_scada_workbook(scada_buffer)
    assert scada_parsed.is_valid
    assert scada_parsed.gateway["mqtt_password"] == synthetic_secret
    assert scada_parsed.gateway["product_key"] == "synthetic-product-key"
    assert all(
        device["device_name"] == "synthetic-device-001"
        for device in scada_parsed.devices
    )
    assert type(scada_parsed.gateway["source_port"]) is int
    assert type(scada_parsed.gateway["mqtt_qos"]) is int


_PRIVATE_IPV4 = re.compile(
    r"(?<![\d.])(?:10(?:\.\d{1,3}){3}|172\.(?:1[6-9]|2\d|3[01])"
    r"(?:\.\d{1,3}){2}|192\.168(?:\.\d{1,3}){2})(?![\d.])"
)
_IDENTIFIER_EXAMPLE_FIELDS = {
    "code",
    "device_a_tag",
    "device_code",
    "device_name",
    "mqtt_client_id",
    "mqtt_username",
    "opcua_username",
    "product_key",
    "scada_device_name",
    "scada_product_key",
    "site_code",
}


def _string_literals(path: Path) -> list[str]:
    if path.suffix == ".json":
        values: list[str] = []

        def walk(value) -> None:
            if isinstance(value, str):
                values.append(value)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
            elif isinstance(value, dict):
                for item in value.values():
                    walk(item)

        walk(json.loads(path.read_text(encoding="utf-8")))
        return values
    if path.suffix == ".md":
        return [path.read_text(encoding="utf-8")]
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_contract_artifacts_use_synthetic_identifier_examples_only() -> None:
    """Scan fixtures and test AST, not just fields labelled ``secret``."""

    artifact_paths = [
        *CONTRACT_ROOT.glob("*.json"),
        *CONTRACT_ROOT.glob("*.md"),
        *(REPOSITORY_ROOT / "backend/tests/contracts").glob("*.py"),
    ]
    offenders = [
        (str(path.relative_to(REPOSITORY_ROOT)), value)
        for path in artifact_paths
        for value in _string_literals(path)
        if _PRIVATE_IPV4.search(value)
    ]
    assert offenders == []

    contract = load_contract("excel-v1.json")
    specs = [
        spec
        for schema in contract["v2"]["schemas"].values()
        for section in ("device", "point")
        for spec in schema[section]
    ] + contract["legacy"]["field_specs"]
    for spec in specs:
        example = spec["example"]
        if example is None:
            continue
        if spec["name"] in _IDENTIFIER_EXAMPLE_FIELDS:
            assert "synthetic" in str(example).lower(), (spec["name"], example)
        if spec["name"] == "source_ip":
            if str(example).endswith(".example.com"):
                continue
            address = ipaddress.ip_address(example)
            assert address in ipaddress.ip_network("192.0.2.0/24")
        if spec["name"] == "endpoint_url":
            assert "192.0.2." in example or ".example." in example
