"""Unit tests for ExcelImportService."""
import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import pandas as pd
from decimal import Decimal

from configuration import models
from configuration.services.importer import (
    ExcelImportService,
    ImportSummary,
    process_excel,
)
from tests.fixtures.factories import *


@pytest.fixture
def sample_excel_file(tmp_path):
    """Create a sample Excel file for testing.

    Schema is the post-refactor ``protocol_type``-driven layout:

    * Modbus rows must carry ``address`` (the importer used to accept
      ``source_addr`` but the schema-driven validator now reads
      ``address`` per ``ModbusTCPProtocol.POINT_FIELDS``).
    * MQTT rows must carry ``mqtt_topics`` on the device side (declared
      ``required=True`` by ``MQTTProtocol.DEVICE_FIELDS``).

    See ``acquisition/protocols/modbus.py`` and ``acquisition/protocols/mqtt.py``
    for the canonical field declarations.
    """
    excel_path = tmp_path / "test_import.xlsx"

    data = {
        "protocol_type": ["modbus_tcp", "modbus_tcp", "mqtt"],
        "source_ip": ["192.168.1.100", "192.168.1.101", "192.168.1.102"],
        "source_port": [502, 502, 1883],
        "code": ["TEMP_001", "PRESS_001", "FLOW_001"],
        "description": ["温度1", "压力1", "流量1"],
        "unit": ["°C", "MPa", "m3/h"],
        "data_type": ["float32", "float32", "float"],
        "address": ["D100", "D200", "topic/flow"],
        "mqtt_topics": ["", "", "sensor/flow"],
        "fs": [1.0, 1.0, 0.5],
        "coefficient": [1.0, 0.1, 1.0],
        "precision": [2, 3, 2],
        "device_name": ["Device1", "Device1", "Device2"],
        "device_a_tag": ["DEV_001", "DEV_001", "DEV_002"],
    }

    df = pd.DataFrame(data)
    df.to_excel(excel_path, index=False)
    return excel_path


@pytest.fixture
def incomplete_excel_file(tmp_path):
    """Create an Excel file missing required columns."""
    excel_path = tmp_path / "incomplete.xlsx"

    data = {
        "protocol_type": ["modbus_tcp"],
        "source_ip": ["192.168.1.100"],
        # Missing source_port and en_name
    }

    df = pd.DataFrame(data)
    df.to_excel(excel_path, index=False)
    return excel_path


@pytest.fixture
def create_import_job():
    """Factory for import jobs."""
    def _create(status="pending", **kwargs):
        return models.ImportJob.objects.create(
            source_name=kwargs.get("source_name", "test.xlsx"),
            triggered_by=kwargs.get("triggered_by", "test"),
            status=status,
            summary=kwargs.get("summary", {}),
        )
    return _create


@pytest.mark.django_db
class TestImportSummary:
    """Tests for ImportSummary dataclass."""

    def test_import_summary_creation(self):
        """Test creating an ImportSummary."""
        summary = ImportSummary(
            rows_parsed=100,
            created_points=50,
            updated_points=30,
            connection_count=3,
        )

        assert summary.rows_parsed == 100
        assert summary.created_points == 50
        assert summary.connection_count == 3
        assert summary.is_successful is True

    def test_import_summary_with_errors(self):
        """Test ImportSummary with errors."""
        summary = ImportSummary(errors=["Error 1", "Error 2"])

        assert summary.is_successful is False
        assert len(summary.errors) == 2

    def test_import_summary_to_dict(self):
        """Test converting ImportSummary to dict."""
        summary = ImportSummary(rows_parsed=100, warnings=["Warning 1"])
        result = summary.to_dict()

        assert result["rows_parsed"] == 100
        assert "Warning 1" in result["warnings"]


@pytest.mark.django_db
class TestExcelImportServiceLoadDataframe:
    """Tests for ExcelImportService.load_dataframe."""

    def test_load_dataframe_success(self, create_import_job, sample_excel_file):
        """Test successful dataframe loading."""
        job = create_import_job()
        service = ExcelImportService(job, sample_excel_file)

        df = service.load_dataframe()

        assert len(df) == 3
        assert "protocol_type" in df.columns

    def test_load_dataframe_file_not_found(self, create_import_job):
        """Test loading non-existent file."""
        job = create_import_job()
        service = ExcelImportService(job, Path("/nonexistent/file.xlsx"))

        summary = service.run_validation()

        assert summary.is_successful is False
        assert any("不存在" in e for e in summary.errors)


@pytest.mark.django_db
class TestExcelImportServiceValidation:
    """Tests for ExcelImportService.run_validation."""

    def test_run_validation_success(self, create_import_job, sample_excel_file):
        """Test successful validation."""
        job = create_import_job()
        service = ExcelImportService(job, sample_excel_file)

        summary = service.run_validation()

        assert summary.is_successful is True
        assert summary.rows_parsed == 3
        assert summary.connection_count == 3
        assert summary.device_tag_count >= 0

    def test_run_validation_missing_columns(self, create_import_job, incomplete_excel_file):
        """Test validation with missing columns."""
        job = create_import_job()
        service = ExcelImportService(job, incomplete_excel_file)

        summary = service.run_validation()

        assert summary.is_successful is False
        assert any("缺少必要列" in e for e in summary.errors)

    def test_run_validation_no_connections(self, create_import_job, tmp_path):
        """Rows missing ``protocol_type`` are flagged as row errors.

        The schema-driven validator now treats ``protocol_type`` as the very
        first gate — without it we cannot pick a ``BaseProtocol`` subclass to
        run further field validation against, so the row never produces a
        connection key and the row is recorded in ``row_errors``.
        """
        excel_path = tmp_path / "no_conn.xlsx"

        # Data with missing protocol_type — the post-refactor importer
        # still requires `code` (BASE_REQUIRED_COLUMNS), so include it.
        data = {
            "protocol_type": [None],
            "source_ip": [None],
            "source_port": [None],
            "code": ["POINT_001"],
        }
        df = pd.DataFrame(data)
        df.to_excel(excel_path, index=False)

        job = create_import_job()
        service = ExcelImportService(job, excel_path)

        summary = service.run_validation()

        assert summary.connection_count == 0
        assert summary.is_successful is False
        # The row gets a RowError pointing at ``protocol_type``.
        assert any(
            err.column == "protocol_type" and "缺少协议类型" in err.message
            for err in summary.row_errors
        )


@pytest.mark.django_db
class TestExcelImportServiceCollectConnections:
    """Connection / device-tag collection.

    The legacy private classmethods ``_collect_connections`` and
    ``_collect_device_tags`` were inlined into :meth:`run_validation` after the
    schema-driven refactor — connections are now keyed by each protocol's
    ``IDENTITY_FIELDS`` (not a hard-coded ``(protocol, ip, port)`` tuple) so
    they can't be tested independently of a protocol class. We exercise the
    same behaviour through the public ``run_validation`` API.
    """

    def test_run_validation_dedupes_connections_by_identity(
        self, create_import_job, tmp_path,
    ):
        """Two rows with the same modbus identity collapse to one connection."""
        excel_path = tmp_path / "dedup.xlsx"
        # Two modbus rows sharing source_ip+source_port → one connection;
        # one mqtt row → a second connection. Three rows, two connections.
        data = {
            "protocol_type": ["modbus_tcp", "modbus_tcp", "mqtt"],
            "source_ip": ["192.168.1.100", "192.168.1.100", "192.168.1.102"],
            "source_port": [502, 502, 1883],
            "code": ["P1", "P2", "P3"],
            "address": ["D100", "D200", "topic/p3"],
            "data_type": ["int16", "int16", "float"],
            "mqtt_topics": ["", "", "sensor/p3"],
        }
        pd.DataFrame(data).to_excel(excel_path, index=False)

        job = create_import_job()
        summary = ExcelImportService(job, excel_path).run_validation()

        assert summary.is_successful is True
        assert summary.connection_count == 2

    def test_run_validation_collects_device_tags(
        self, create_import_job, tmp_path,
    ):
        """``device_name`` / ``device_a_tag`` populate ``device_tags`` metadata."""
        excel_path = tmp_path / "tags.xlsx"
        data = {
            "protocol_type": ["modbus_tcp"],
            "source_ip": ["192.168.1.100"],
            "source_port": [502],
            "code": ["P1"],
            "address": ["D100"],
            "data_type": ["int16"],
            "device_name": ["Device A"],
            "device_a_tag": ["DEV_A"],
        }
        pd.DataFrame(data).to_excel(excel_path, index=False)

        job = create_import_job()
        summary = ExcelImportService(job, excel_path).run_validation()

        assert summary.is_successful is True
        # device_name takes precedence over device_a_tag in collection.
        assert summary.device_tag_count == 1
        assert "Device A" in summary.metadata["device_tags"]


@pytest.mark.django_db
class TestExcelImportServicePersistSummary:
    """Tests for persist_summary method."""

    def test_persist_summary_success(self, create_import_job, sample_excel_file):
        """Test persisting successful summary."""
        job = create_import_job()
        service = ExcelImportService(job, sample_excel_file)

        summary = ImportSummary(rows_parsed=100, created_points=50)
        service.persist_summary(summary)

        job.refresh_from_db()
        assert job.status == models.ImportJob.STATUS_VALIDATED
        assert job.summary["rows_parsed"] == 100

    def test_persist_summary_failure(self, create_import_job, sample_excel_file):
        """Test persisting failed summary."""
        job = create_import_job()
        service = ExcelImportService(job, sample_excel_file)

        summary = ImportSummary(errors=["Test error"])
        service.persist_summary(summary)

        job.refresh_from_db()
        assert job.status == models.ImportJob.STATUS_FAILED


@pytest.mark.django_db
class TestExcelImportServiceComputeDiff:
    """Tests for compute_diff method."""

    def test_compute_diff_empty_site(self, create_import_job, sample_excel_file):
        """Test computing diff for new site."""
        job = create_import_job()
        service = ExcelImportService(job, sample_excel_file)

        diff = service.compute_diff(site_code="new_site")

        assert diff["site_code"] == "new_site"
        assert len(diff["connections"]["to_create"]) == 3  # All new
        assert len(diff["connections"]["to_remove"]) == 0  # None to remove

    def test_compute_diff_existing_site(self, create_import_job, sample_excel_file, create_site, create_device, create_point):
        """Test computing diff for existing site with same data.

        ``compute_diff`` matches existing devices by ``Device.code`` — which
        the importer derives from each protocol's ``IDENTITY_FIELDS`` via
        ``_device_code``. For the modbus row in ``sample_excel_file``
        (``source_ip=192.168.1.100``, ``source_port=502``) that comes out as
        ``"modbus_tcp-192.168.1.100-502"`` (slave_id is absent → trimmed).
        Pre-create the device under that exact code so the diff sees it as
        ``existing`` rather than ``to_create``.
        """
        site = create_site(code="existing_site")
        device = create_device(
            site=site,
            code="modbus_tcp-192.168.1.100-502",
            protocol="modbus_tcp",
            ip="192.168.1.100",
            port=502,
        )
        point = create_point(device=device, code="TEMP_001")

        job = create_import_job()
        service = ExcelImportService(job, sample_excel_file)

        diff = service.compute_diff(site_code="existing_site")

        # TEMP_001 should be existing, others new
        assert len(diff["connections"]["existing"]) >= 1


@pytest.mark.django_db
class TestExcelImportServiceApply:
    """Tests for apply method."""

    def test_apply_merge_mode(self, create_import_job, sample_excel_file):
        """Test applying in merge mode."""
        job = create_import_job()
        service = ExcelImportService(job, sample_excel_file)

        result = service.apply(site_code="merge_test_site", mode="merge")

        assert result["mode"] == "merge"
        assert result["device_created"] == 3
        assert result["point_created"] == 3
        assert job.status == models.ImportJob.STATUS_APPLIED

    def test_apply_append_mode(self, create_import_job, sample_excel_file):
        """Test applying in append mode."""
        job = create_import_job()
        service = ExcelImportService(job, sample_excel_file)

        # Apply twice should skip existing
        service.apply(site_code="append_test_site", mode="append")
        result = service.apply(site_code="append_test_site", mode="append")

        # Second apply should skip all
        assert result["device_skipped"] == 3
        assert result["point_skipped"] == 3

    def test_apply_replace_mode_deletes_existing(self, create_import_job, sample_excel_file, create_site, create_device):
        """Test that replace mode deletes existing data."""
        site = create_site(code="replace_test_site")
        old_device = create_device(site=site, protocol="modbus_tcp", ip="192.168.0.1", port=502)

        job = create_import_job()
        service = ExcelImportService(job, sample_excel_file)

        result = service.apply(site_code="replace_test_site", mode="replace")

        # Old device should be deleted
        assert models.Device.objects.filter(site=site, ip_address="192.168.0.1").count() == 0
        # New devices should be created
        assert result["device_created"] >= 1

    def test_apply_creates_templates(self, create_import_job, sample_excel_file):
        """Test that apply creates point templates."""
        job = create_import_job()
        service = ExcelImportService(job, sample_excel_file)

        service.apply(site_code="template_test_site", mode="merge")

        # Templates should be created
        assert models.PointTemplate.objects.filter(name="温度1").exists()
        assert models.PointTemplate.objects.filter(name="压力1").exists()

    def test_apply_creates_tasks(self, create_import_job, sample_excel_file):
        """Test that apply creates tasks."""
        job = create_import_job()
        service = ExcelImportService(job, sample_excel_file)

        result = service.apply(site_code="task_test_site", mode="merge")

        assert len(result["task_versions"]) >= 1
        # Tasks should be created
        task_codes = [t.code for t in models.AcqTask.objects.all()]
        assert any("task-" in code for code in task_codes)

    def test_apply_creates_versions(self, create_import_job, sample_excel_file):
        """Test that apply creates config versions."""
        job = create_import_job()
        service = ExcelImportService(job, sample_excel_file)

        result = service.apply(site_code="version_test_site", mode="merge")

        assert len(result["task_versions"]) >= 1
        for version_id in result["task_versions"]:
            version = models.ConfigVersion.objects.get(id=version_id)
            assert version.payload is not None


@pytest.mark.django_db
class TestProcessExcel:
    """Tests for process_excel shortcut function."""

    def test_process_excel_validates_and_persists(self, create_import_job, sample_excel_file):
        """Test process_excel validates and persists summary."""
        job = create_import_job()

        summary = process_excel(job, sample_excel_file, site_code="process_test")

        assert summary.is_successful is True
        job.refresh_from_db()
        assert job.status == models.ImportJob.STATUS_VALIDATED
        assert "site_code" in job.summary.get("metadata", {})


@pytest.mark.django_db
class TestImportJobStatusTransitions:
    """Tests for ImportJob status transitions."""

    def test_status_transitions(self, create_import_job, sample_excel_file):
        """Test ImportJob status changes through workflow."""
        job = create_import_job(status="pending")
        assert job.status == "pending"

        # Validate
        service = ExcelImportService(job, sample_excel_file)
        summary = service.run_validation()
        service.persist_summary(summary)
        job.refresh_from_db()
        assert job.status == "validated"

        # Apply
        service.apply(site_code="workflow_test_site", mode="merge")
        job.refresh_from_db()
        assert job.status == "applied"
