"""Tests for the device-level Excel export (Agent H — 设备级 Excel 导出).

Import has been symmetric for a while (generic 40-column single-sheet +
SCADA's own two-sheet template/import), but export only covered SCADA
gateways and config-version snapshots — non-SCADA protocols could only be
imported, never exported back out for editing. ``GET /api/config/devices/
export/`` closes that gap: same column layout as the importer template
(``_column_specs``), so the file it produces is importable byte-for-byte via
``/api/config/import-jobs/``.

Two landmines this suite pins down:

* **The pandas float pitfall.** The moment a column has *any* blank cell
  across the sheet, pandas widens the whole column to float64 — 502 becomes
  502.0. The importer's ``_coerce_one``/``_device_code`` already guard
  against this on the way IN; the round-trip test below (import → export →
  wipe → reimport the export → export again) asserts the export doesn't
  reintroduce it on the way OUT (device codes must stay stable, ints must
  stay ints — no cell should read back as ``503.0``).
* **SCADA exclusion.** SCADA devices are provisioned through ``ScadaGateway``
  — their broker/credential fields live on the gateway row, not
  ``device.metadata`` — so a generic export row for them would be missing
  required connection fields and silently fail re-import. This endpoint must
  never emit scada rows, even if ``protocols=scada`` is explicitly requested.
"""
from __future__ import annotations

import io

import pandas as pd
import pytest
from openpyxl import load_workbook
from rest_framework import status
from rest_framework.test import APIClient

from configuration import models
from configuration.services.exporter import ExcelExportService
from configuration.services.importer import ExcelImportService
from tests.fixtures.factories import *  # noqa: F401,F403

XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def api_client():
    return APIClient()


# ``create_import_job`` comes from tests.fixtures.factories (imported above via *).


def _generic_import_bytes() -> bytes:
    """Two modbus_tcp points on one device + one mqtt point on another.

    Deliberately leaves ``slave_id``/``num`` blank on the mqtt row (columns
    those protocols don't use) — with ``pd.DataFrame.to_excel`` that blank
    NaN widens the *whole* column to float64, exactly the pandas pitfall
    this module's docstring calls out. If the round trip below still lands
    on stable int device codes / int cells, the importer + exporter are both
    handling it correctly.
    """
    data = {
        "protocol_type": ["modbus_tcp", "modbus_tcp", "mqtt"],
        "source_ip": ["192.168.1.100", "192.168.1.100", "192.168.1.102"],
        "source_port": [502, 502, 1883],
        "slave_id": [1, 1, None],
        "code": ["TEMP_001", "PRESS_001", "FLOW_001"],
        "description": ["温度1", "压力1", "流量1"],
        "unit": ["°C", "MPa", "m3/h"],
        "data_type": ["float32", "float32", "float"],
        "address": ["D100", "D200", "topic/flow"],
        "mqtt_topics": [None, None, "sensor/flow"],
        "num": [1, 1, None],
        "coefficient": [1.0, 0.1, 1.0],
        "device_name": ["Device1", "Device1", "Device2"],
        "device_a_tag": ["DEV_001", "DEV_001", "DEV_002"],
    }
    buf = io.BytesIO()
    pd.DataFrame(data).to_excel(buf, index=False)
    return buf.getvalue()


def _import_bytes(tmp_path, name: str, content: bytes) -> "ExcelImportService":
    path = tmp_path / name
    path.write_bytes(content)
    return path


def _apply_import(create_import_job, tmp_path, content: bytes, *, site_code: str, mode: str = "merge"):
    job = create_import_job()
    path = _import_bytes(tmp_path, f"{job.id}.xlsx", content)
    service = ExcelImportService(job, path)
    return service.apply(site_code=site_code, mode=mode)


def _snapshot():
    """(device_code, protocol, sorted point codes) triples, order-independent."""
    out = set()
    for device in models.Device.objects.prefetch_related("points"):
        codes = tuple(sorted(device.points.values_list("code", flat=True)))
        out.add((device.code, device.protocol, codes))
    return out


# ---------------------------------------------------------------------------
# round trip: import -> export -> wipe -> reimport export -> export again
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDeviceExportRoundTrip:
    def test_round_trip_preserves_devices_and_points(self, create_import_job, tmp_path):
        result1 = _apply_import(create_import_job, tmp_path, _generic_import_bytes(), site_code="rt-site")
        assert result1["device_created"] == 2
        assert result1["point_created"] == 3
        snapshot1 = _snapshot()

        export1 = ExcelExportService().export_devices()

        # 清库
        models.Device.objects.all().delete()
        assert models.Device.objects.count() == 0

        result2 = _apply_import(create_import_job, tmp_path, export1, site_code="rt-site")
        assert result2["device_created"] == 2
        assert result2["point_created"] == 3
        snapshot2 = _snapshot()

        # code 幂等：设备 code / 协议 / 测点 code 集合与第一次导入完全一致
        assert snapshot2 == snapshot1

        export2 = ExcelExportService().export_devices()

        # 两次导出内容在“去掉时间戳/无关空白”意义上一致：逐行对比关键列。
        rows1 = _read_data_rows(export1)
        rows2 = _read_data_rows(export2)
        assert sorted(rows1) == sorted(rows2)

    def test_round_trip_numeric_cells_stay_int_no_dot_zero(self, create_import_job, tmp_path):
        """The pandas-float pitfall: int fields must not grow a ``.0``."""
        _apply_import(create_import_job, tmp_path, _generic_import_bytes(), site_code="rt-numeric")
        export1 = ExcelExportService().export_devices()

        wb = load_workbook(io.BytesIO(export1), data_only=True)
        ws = wb["采集点配置"]
        header = [c.value for c in ws[1]]
        col = {name: idx for idx, name in enumerate(header)}

        int_field_names = [n for n in ("source_port", "slave_id", "num") if n in col]
        assert int_field_names, "expected at least one int-kind column in the export"

        for row in ws.iter_rows(min_row=2, values_only=True):
            for name in int_field_names:
                value = row[col[name]]
                if value is None:
                    continue
                assert isinstance(value, int), (
                    f"column {name!r} exported as {type(value).__name__} ({value!r}), "
                    "expected a clean int — pandas float pitfall regression"
                )

        # Device.code itself must not carry a stray ".0" from a float port/slave_id.
        for code in models.Device.objects.values_list("code", flat=True):
            assert ".0-" not in code and not code.endswith(".0"), code


def _read_data_rows(xlsx_bytes: bytes) -> list[tuple]:
    wb = load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    ws = wb["采集点配置"]
    header = [c.value for c in ws[1]]
    rows = []
    for raw in ws.iter_rows(min_row=2, values_only=True):
        if raw is None or all(v is None for v in raw):
            continue
        rows.append(tuple(zip(header, raw)))
    return rows


# ---------------------------------------------------------------------------
# protocol filtering + SCADA exclusion
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDeviceExportFiltering:
    def test_protocols_filter_narrows_devices(self, create_import_job, tmp_path):
        _apply_import(create_import_job, tmp_path, _generic_import_bytes(), site_code="filter-site")

        content = ExcelExportService().export_devices(protocols=["modbus_tcp"])
        rows = _read_data_rows(content)
        protocols_seen = {dict(r)["protocol_type"] for r in rows}
        assert protocols_seen == {"modbus_tcp"}

    def test_no_protocols_param_exports_all_non_scada(self, create_import_job, tmp_path, create_site, create_device, create_point):
        _apply_import(create_import_job, tmp_path, _generic_import_bytes(), site_code="filter-site2")
        site = create_site(code="scada-site")
        scada_device = create_device(site=site, protocol="scada", ip="10.0.0.9", port=8883)
        scada_device.metadata = {"scada_device_name": "A0201010001150403"}
        scada_device.save(update_fields=["metadata"])
        create_point(device=scada_device, code="N001")

        content = ExcelExportService().export_devices()
        rows = _read_data_rows(content)
        protocols_seen = {dict(r)["protocol_type"] for r in rows}
        assert "scada" not in protocols_seen
        assert {"modbus_tcp", "mqtt"} <= protocols_seen

    def test_explicit_scada_request_still_excluded(self, create_site, create_device, create_point):
        site = create_site(code="scada-only-site")
        scada_device = create_device(site=site, protocol="scada", ip="10.0.0.9", port=8883)
        scada_device.metadata = {"scada_device_name": "A0201010001150403"}
        scada_device.save(update_fields=["metadata"])
        create_point(device=scada_device, code="N001")

        # Asking specifically for scada must not leak scada rows — the format
        # can't round-trip through the generic importer for it.
        content = ExcelExportService().export_devices(protocols=["scada", "modbus_tcp"])
        rows = _read_data_rows(content)
        assert rows == []


# ---------------------------------------------------------------------------
# HTTP wiring
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDeviceExportEndpoint:
    def test_endpoint_returns_xlsx(self, api_client, create_import_job, tmp_path):
        _apply_import(create_import_job, tmp_path, _generic_import_bytes(), site_code="http-site")

        resp = api_client.get("/api/config/devices/export/")
        assert resp.status_code == status.HTTP_200_OK
        assert resp["Content-Type"] == XLSX_CONTENT_TYPE
        assert "attachment; filename=" in resp["Content-Disposition"]

        rows = _read_data_rows(resp.content)
        assert len(rows) == 3

    def test_endpoint_protocols_query_param(self, api_client, create_import_job, tmp_path):
        _apply_import(create_import_job, tmp_path, _generic_import_bytes(), site_code="http-site2")

        resp = api_client.get("/api/config/devices/export/?protocols=mqtt")
        assert resp.status_code == status.HTTP_200_OK
        rows = _read_data_rows(resp.content)
        assert len(rows) == 1
        assert dict(rows[0])["protocol_type"] == "mqtt"

    def test_endpoint_empty_db_still_returns_well_formed_file(self, api_client):
        resp = api_client.get("/api/config/devices/export/")
        assert resp.status_code == status.HTTP_200_OK
        wb = load_workbook(io.BytesIO(resp.content))
        assert "采集点配置" in wb.sheetnames
        assert "协议说明" in wb.sheetnames
        # header row present, no data rows
        ws = wb["采集点配置"]
        assert ws.cell(row=1, column=1).value == "protocol_type"
