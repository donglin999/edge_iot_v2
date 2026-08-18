"""Workbook layout contract for v2, legacy and SCADA import/export."""
from __future__ import annotations

import io
from contextlib import contextmanager
from dataclasses import fields
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from openpyxl import load_workbook
from rest_framework.test import APIClient

from acquisition.protocols import ProtocolRegistry
from acquisition.protocols.base import FieldSpec
from acquisition.services import templates as legacy_templates
from configuration.services import protocol_excel, scada_excel

from .helpers import field_spec, load_contract


def _header_values(sheet, row: int) -> list[str]:
    return [cell.value for cell in sheet[row] if cell.value is not None]


@contextmanager
def _baseline_registry(contract: dict):
    """Hide protocols imported dynamically by unrelated tests.

    ``test_default_read_batch`` deliberately imports the not-yet-productized
    Mitsubishi adapter and leaves it registered process-wide. Production never
    imports that module from ``acquisition.protocols.__init__``.  Contract
    checks can run late in the full suite, so compare against the clean startup
    registry instead of inheriting test-order pollution.
    """

    names = sorted([*contract["v2"]["protocols"], "scada"])
    protocols = {name: ProtocolRegistry.get(name) for name in names}
    with patch.object(ProtocolRegistry, "_protocols", protocols):
        yield


def test_v2_field_specs_and_real_template_layout_match_snapshot() -> None:
    root_contract = load_contract("excel-v1.json")
    contract = root_contract["v2"]
    field_names = [item.name for item in fields(FieldSpec)]
    with _baseline_registry(root_contract):
        actual_protocols = [item["name"] for item in protocol_excel.production_protocols()]
        assert actual_protocols == contract["protocols"]

        for name in contract["protocols"]:
            klass = ProtocolRegistry.get(name)
            expected = contract["schemas"][name]
            assert [field_spec(item) for item in protocol_excel._device_columns(klass)] == expected["device"]
            assert [field_spec(item) for item in protocol_excel._point_columns(klass)] == expected["point"]
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
            assert _header_values(workbook["设备"], 2) == [item["name"] for item in expected["device"]]
            assert _header_values(workbook["测点"], 1) == expected_point_labels
            assert _header_values(workbook["测点"], 2) == [item["name"] for item in expected["point"]]


def test_legacy_40_column_layout_matches_snapshot() -> None:
    root_contract = load_contract("excel-v1.json")
    contract = root_contract["legacy"]
    with _baseline_registry(root_contract):
        protocols = ProtocolRegistry.list_protocols()
        specs = legacy_templates._column_specs(protocols)
        assert [field_spec(item) for item in specs] == contract["field_specs"]
        assert all(
            list(item) == [field.name for field in fields(FieldSpec)]
            for item in contract["field_specs"]
        )

        workbook = load_workbook(io.BytesIO(legacy_templates.build_template(protocols)))
        assert workbook.sheetnames == contract["sheets"]
        expected_header = contract["fixed_columns"] + [item["name"] for item in contract["field_specs"]]
        assert _header_values(workbook["采集点配置"], 1) == expected_header


def test_scada_single_header_layout_matches_snapshot() -> None:
    contract = load_contract("excel-v1.json")["scada"]
    workbook = load_workbook(io.BytesIO(scada_excel.build_scada_template()))
    assert workbook.sheetnames == contract["sheets"]
    assert _header_values(workbook["网关服务"], 1) == contract["gateway_columns"]
    assert _header_values(workbook["设备与测点"], 1) == contract["point_columns"]
    assert contract["header_rows"] == 1


def test_v2_templates_parse_in_memory_and_integer_cells_stay_integers() -> None:
    root_contract = load_contract("excel-v1.json")
    with _baseline_registry(root_contract):
        for name in root_contract["v2"]["protocols"]:
            blob = protocol_excel.build_template(name)
            parsed = protocol_excel.parse_workbook(io.BytesIO(blob))
            assert parsed.is_valid, [error.to_dict() for error in parsed.errors]
            assert parsed.protocol == name
            assert parsed.devices
            assert parsed.devices[0].points

        blob = protocol_excel.build_template("modbus_tcp")
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

    blob = protocol_excel.build_template("modbus_tcp")
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

    mqtt_blob = protocol_excel.build_template("mqtt")
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
    gateway_columns = {
        cell.value: cell.column for cell in gateway_sheet[1] if cell.value is not None
    }
    assert type(gateway_sheet.cell(2, gateway_columns["source_port"]).value) is int
    assert type(gateway_sheet.cell(2, gateway_columns["mqtt_qos"]).value) is int
    gateway_sheet.cell(2, gateway_columns["mqtt_password"], synthetic_secret)
    scada_buffer = io.BytesIO()
    scada_workbook.save(scada_buffer)
    scada_workbook.close()
    scada_buffer.seek(0)
    scada_parsed = scada_excel.parse_scada_workbook(scada_buffer)
    assert scada_parsed.is_valid
    assert scada_parsed.gateway["mqtt_password"] == synthetic_secret
    assert type(scada_parsed.gateway["source_port"]) is int
    assert type(scada_parsed.gateway["mqtt_qos"]) is int
