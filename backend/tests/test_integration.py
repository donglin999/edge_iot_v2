"""End-to-end integration tests for the acquisition pipeline.

After the M2 pipeline refactor the read-format-store path is split across:

* ``AcquisitionService.acquire_once`` — reads via ``ProtocolRegistry``,
  returns readings; never touches storage.
* ``InfluxDBSink.consume`` — formats one :class:`Reading` and buffers it.
* ``InfluxDBSink.flush`` — writes the buffer to the configured storage backend.

These tests stitch those three steps together against the in-memory
``MockInfluxDBStorage`` to verify the *whole* path (acquire → format → store)
without the threaded ``run_continuous`` loop, which has its own coverage in
``tests/test_pipeline_watchdog.py``.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from acquisition.services.acquisition_service import AcquisitionService
from acquisition.services.read_plan import Reading
from acquisition.services.sinks import InfluxDBSink
from tests.fixtures.factories import *  # noqa: F401,F403
from tests.mocks.protocols import register_mock_protocols
from tests.mocks.storage import MockInfluxDBStorage, register_mock_storage


@pytest.fixture(autouse=True)
def setup_mocks():
    """Register mock protocols + storage for every test in this module."""
    register_mock_protocols()
    register_mock_storage()
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sink_with_mock_storage(
    session, device_groups, mock_storage,
):
    """Build an :class:`InfluxDBSink` wired to ``mock_storage`` directly.

    Bypasses ``InfluxDBSink._init_storage`` (which would try to instantiate
    the real ``influxdb`` backend via ``StorageRegistry``) so we can inject
    a known mock and assert against ``mock_storage.get_written_data()``.

    Mirrors the ``_make_sink_skeleton`` helper in
    ``tests/test_acquisition_service.py`` but uses a real ``MockInfluxDBStorage``
    rather than a ``MagicMock`` so we exercise the full write path.
    """
    import threading

    sink = InfluxDBSink.__new__(InfluxDBSink)
    sink.session = session
    sink.device_groups = device_groups
    sink._lock = threading.Lock()
    sink._buffer = []
    sink._last_flush = time.time()
    sink._storage = mock_storage
    sink._total_written = 0
    sink._fail_count = 0
    sink._next_flush_at = 0.0
    sink._dropped_total = 0
    sink._flush_in_progress = False
    sink._point_meta = {}
    for _device_id, group in device_groups.items():
        device = group["device"]
        for point in group["points"]:
            sink._point_meta[point["code"]] = {
                "device": device,
                "coefficient": float(point.get("coefficient", 1.0) or 1.0),
                "precision": int(point.get("precision", 2) or 2),
                "template_name": point.get("cn_name") or point.get("description") or "",
                "template_unit": point.get("unit", "") or "",
            }
    return sink


def _feed_readings_through_sink(sink, readings_dicts):
    """Convert ``acquire_once`` row dicts into :class:`Reading`s and consume.

    ``acquire_once`` returns the legacy list-of-dict shape; the sink consumes
    :class:`Reading`. We adapt here so the integration tests only deal with
    one shape.
    """
    for row in readings_dicts:
        reading = Reading(
            point_code=row["code"],
            value=row["value"],
            timestamp_ns=row.get("timestamp", time.time_ns()),
            quality=row.get("quality", "good"),
        )
        sink.consume(reading)


# ---------------------------------------------------------------------------
# End-to-end acquisition (read → format → store)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestEndToEndAcquisition:
    """Verify acquire_once + InfluxDBSink form a working acquisition path."""

    def test_full_acquisition_pipeline(
        self,
        create_site,
        create_device,
        create_point,
        create_task,
        create_session,
    ):
        """Three points off one modbus device flow into formatted storage points."""
        site = create_site(code="FACTORY_01", name="Test Factory")
        device = create_device(
            site=site,
            protocol="mock_modbus",
            ip="192.168.1.100",
            port=502,
            code="MODBUS_001",
            metadata={
                "device_a_tag": "SENSOR_RACK_01",
                "_test_simulated_data": {
                    "TEMP_01": 25.5,
                    "PRESSURE_01": 101.3,
                    "FLOW_01": 150.0,
                },
            },
        )
        temp_point = create_point(device=device, code="TEMP_01", address="D100")
        pressure_point = create_point(device=device, code="PRESSURE_01", address="D101")
        flow_point = create_point(device=device, code="FLOW_01", address="D102")

        task = create_task(
            code="SENSOR_MONITORING",
            points=[temp_point, pressure_point, flow_point],
        )
        session = create_session(task=task)

        service = AcquisitionService(task, session)
        result = service.acquire_once()

        assert result["status"] == "completed"
        assert result["points_read"] == 3
        assert result["errors"] == []

        # Hand the readings off to a sink wired to in-memory storage so we
        # can verify the formatted-and-written shape end-to-end.
        mock_storage = MockInfluxDBStorage({})
        mock_storage.connect()
        sink = _make_sink_with_mock_storage(session, service.device_groups, mock_storage)

        _feed_readings_through_sink(sink, result["data"])
        sink.flush()

        written = mock_storage.get_written_data()
        assert len(written) == 3

        temp_data = next((d for d in written if "TEMP_01" in d["fields"]), None)
        assert temp_data is not None
        # measurement falls back to device.metadata['device_a_tag'] when set.
        assert temp_data["measurement"] == "SENSOR_RACK_01"
        assert temp_data["tags"]["site"] == "FACTORY_01"
        assert temp_data["tags"]["device"] == "MODBUS_001"
        assert temp_data["fields"]["TEMP_01"] == 25.5

    def test_multi_device_acquisition(
        self,
        create_site,
        create_device,
        create_point,
        create_task,
        create_session,
    ):
        """Two devices on different protocols both produce readings + storage points."""
        site = create_site()
        modbus_device = create_device(
            site=site,
            protocol="mock_modbus",
            code="MODBUS_001",
            metadata={"_test_simulated_data": {"MB_P1": 100}},
        )
        plc_device = create_device(
            site=site,
            protocol="mock_plc",
            code="PLC_001",
            metadata={"_test_simulated_data": {"PLC_P1": 200}},
        )

        mb_point = create_point(device=modbus_device, code="MB_P1")
        plc_point = create_point(device=plc_device, code="PLC_P1")

        task = create_task(points=[mb_point, plc_point])
        session = create_session(task=task)

        service = AcquisitionService(task, session)
        result = service.acquire_once()

        assert result["status"] == "completed"
        assert result["points_read"] == 2

        mock_storage = MockInfluxDBStorage({})
        mock_storage.connect()
        sink = _make_sink_with_mock_storage(session, service.device_groups, mock_storage)
        _feed_readings_through_sink(sink, result["data"])
        sink.flush()

        codes = [list(d["fields"].keys())[0] for d in mock_storage.get_written_data()]
        assert "MB_P1" in codes
        assert "PLC_P1" in codes

    def test_partial_failure_handling(
        self,
        create_device,
        create_point,
        create_task,
        create_session,
    ):
        """A failing device must not stop the rest of the task from succeeding."""
        good_device = create_device(
            protocol="mock_modbus",
            code="GOOD_DEV",
            metadata={"_test_simulated_data": {"P1": 100}},
        )
        bad_device = create_device(
            protocol="mock_modbus",
            code="BAD_DEV",
            metadata={"_test_connection_fail": True},
        )

        good_point = create_point(device=good_device, code="P1")
        bad_point = create_point(device=bad_device, code="P2")

        task = create_task(points=[good_point, bad_point])
        session = create_session(task=task)

        service = AcquisitionService(task, session)
        result = service.acquire_once()

        # The good device succeeded; the bad one surfaces in errors[].
        assert result["status"] == "completed"
        assert result["points_read"] == 1
        assert len(result["errors"]) == 1
        assert result["errors"][0]["device"] == "BAD_DEV"

        mock_storage = MockInfluxDBStorage({})
        mock_storage.connect()
        sink = _make_sink_with_mock_storage(session, service.device_groups, mock_storage)
        _feed_readings_through_sink(sink, result["data"])
        sink.flush()

        written = mock_storage.get_written_data()
        assert len(written) == 1
        assert "P1" in list(written[0]["fields"].keys())

    def test_storage_failure_handling(
        self,
        create_device,
        create_point,
        create_task,
        create_session,
    ):
        """A failing storage write must not raise; the acquire call still succeeds."""
        device = create_device(
            protocol="mock_modbus",
            metadata={"_test_simulated_data": {"P1": 100}},
        )
        point = create_point(device=device, code="P1")
        task = create_task(points=[point])
        session = create_session(task=task)

        service = AcquisitionService(task, session)
        result = service.acquire_once()
        assert result["points_read"] == 1

        # Storage that throws on every write — sink must swallow + retry.
        mock_storage = MockInfluxDBStorage({"_test_write_fail": True})
        mock_storage.connect()
        sink = _make_sink_with_mock_storage(session, service.device_groups, mock_storage)
        _feed_readings_through_sink(sink, result["data"])

        # Must not raise.
        sink.flush()

        # Buffer is retained for retry; total_written stays 0.
        assert sink.total_written == 0
        assert sink._fail_count == 1
        assert len(sink._buffer) == 1


@pytest.mark.django_db
class TestProtocolInteroperability:
    """ModbusTCP, PLC, and MQTT all flowing through the same task / session."""

    def test_mixed_protocol_acquisition(
        self,
        create_device,
        create_point,
        create_task,
        create_session,
    ):
        modbus_dev = create_device(
            protocol="mock_modbus",
            code="MB",
            metadata={"_test_simulated_data": {"MB_TEMP": 25.0}},
        )
        plc_dev = create_device(
            protocol="mock_plc",
            code="PLC",
            metadata={"_test_simulated_data": {"PLC_PRESSURE": 100.0}},
        )
        mqtt_dev = create_device(
            protocol="mock_mqtt",
            code="MQTT",
            metadata={
                "_test_messages": [
                    {
                        "code": "MQTT_SENSOR",
                        "value": 60.0,
                        "timestamp": 1234567890000000000,
                        "quality": "good",
                    }
                ]
            },
        )

        mb_point = create_point(device=modbus_dev, code="MB_TEMP")
        plc_point = create_point(device=plc_dev, code="PLC_PRESSURE")
        mqtt_point = create_point(device=mqtt_dev, code="MQTT_SENSOR")

        task = create_task(points=[mb_point, plc_point, mqtt_point])
        session = create_session(task=task)

        service = AcquisitionService(task, session)
        result = service.acquire_once()

        assert result["status"] == "completed"
        assert result["points_read"] == 3
        assert result["errors"] == []

        mock_storage = MockInfluxDBStorage({})
        mock_storage.connect()
        sink = _make_sink_with_mock_storage(session, service.device_groups, mock_storage)
        _feed_readings_through_sink(sink, result["data"])
        sink.flush()

        assert len(mock_storage.get_written_data()) == 3


@pytest.mark.django_db
class TestDataFormatting:
    """Point template metadata propagates into storage tags."""

    def test_point_template_applied(
        self,
        create_device,
        create_point,
        create_point_template,
        create_task,
        create_session,
    ):
        # M5 schema: unit + Chinese name are written as string fields, not tags.
        template = create_point_template(
            name="温度",
            unit="°C",
            data_type="float",
            coefficient="0.1",
            precision=2,
        )
        device = create_device(
            protocol="mock_modbus",
            metadata={"_test_simulated_data": {"TEMP": 250}},  # *0.1 = 25.0
        )
        point = create_point(device=device, code="TEMP", template=template)

        task = create_task(points=[point])
        session = create_session(task=task)

        service = AcquisitionService(task, session)
        result = service.acquire_once()

        mock_storage = MockInfluxDBStorage({})
        mock_storage.connect()
        sink = _make_sink_with_mock_storage(session, service.device_groups, mock_storage)
        _feed_readings_through_sink(sink, result["data"])
        sink.flush()

        written = mock_storage.get_written_data()
        assert len(written) == 1
        data = written[0]
        # M5: cn_name / unit moved out of the (low-cardinality) tag set.
        assert "cn_name" not in data["tags"]
        assert "unit" not in data["tags"]
        assert data["fields"]["cn_name"] == "温度"
        assert data["fields"]["unit"] == "°C"
        # Template coefficient (0.1) is applied: raw 250 → 25.0.
        assert data["fields"]["TEMP"] == 25.0
