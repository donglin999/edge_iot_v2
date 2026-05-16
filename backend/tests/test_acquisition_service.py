"""Unit tests for the acquisition service layer.

After the pipeline refactor (Plan B / M2), most heavy lifting moved out of
``AcquisitionService`` into:

* :class:`acquisition.services.pipeline.AcquisitionPipeline` /
  :class:`acquisition.services.pipeline.ReadWorker` — owns the per-device
  read loops and protocol I/O.
* :class:`acquisition.services.sinks.InfluxDBSink` — owns formatting,
  buffering, and storage writes.

So this file's responsibility shrank to the surface that *still* lives on
``AcquisitionService``:

* ``__init__`` / ``_group_points_by_device`` — the input shape every other
  layer consumes.
* ``acquire_once`` — single-shot read used by the ad-hoc Celery task; goes
  straight through ``ProtocolRegistry`` and never touches storage.
* ``_should_continue`` — session-status gate for the continuous loop.

Coverage that used to live here has been re-homed:

* ``_format_for_storage``  -> :class:`InfluxDBSink.consume` (verified below
  as `TestInfluxDBSinkFormatting`).
* ``_write_to_storage``    -> :class:`InfluxDBSink.flush` (verified below as
  `TestInfluxDBSinkFlush`).
* ``_init_storages``       -> :meth:`InfluxDBSink._init_storage` (verified
  below as `TestInfluxDBSinkInitialization`).
* ``_get_cycle_interval``  -> :class:`ReadWorker` ``cycle_interval``
  (covered by ``tests/test_pipeline_watchdog.py`` and
  ``tests/test_read_plan.py``).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from acquisition import models as acq_models
from acquisition.services.acquisition_service import AcquisitionService
from acquisition.services.read_plan import Reading
from acquisition.services.sinks import InfluxDBSink
from tests.fixtures.factories import *  # noqa: F401,F403
from tests.mocks.protocols import register_mock_protocols
from tests.mocks.storage import register_mock_storage


@pytest.fixture(autouse=True)
def setup_mocks():
    """Register mock protocols + storage for every test in this module."""
    register_mock_protocols()
    register_mock_storage()
    yield


# ---------------------------------------------------------------------------
# AcquisitionService — what's left on the class after the refactor
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAcquisitionService:
    """Lifecycle / grouping logic that still lives on AcquisitionService."""

    def test_service_initialization(self, create_task, create_session):
        """Service stores task, session, and builds device_groups eagerly."""
        task = create_task()
        session = create_session(task=task)

        service = AcquisitionService(task, session)

        assert service.task == task
        assert service.session == session
        assert len(service.device_groups) > 0

    def test_group_points_by_device(
        self, create_task, create_device, create_point, create_session,
    ):
        """Points are bucketed per device and produce one group per device."""
        device1 = create_device(code="DEV_001", protocol="mock_modbus")
        device2 = create_device(code="DEV_002", protocol="mock_plc")

        point1 = create_point(device=device1, code="P1")
        point2 = create_point(device=device1, code="P2")
        point3 = create_point(device=device2, code="P3")

        task = create_task(points=[point1, point2, point3])
        session = create_session(task=task)

        service = AcquisitionService(task, session)

        assert len(service.device_groups) == 2

        group1 = service.device_groups[device1.id]
        assert group1["device"] == device1
        assert len(group1["points"]) == 2

        group2 = service.device_groups[device2.id]
        assert group2["device"] == device2
        assert len(group2["points"]) == 1

    def test_acquire_once_success(
        self,
        create_task,
        create_session,
        create_device,
        create_point,
    ):
        """``acquire_once`` reads via ProtocolRegistry — no storage involved."""
        device = create_device(
            protocol="mock_modbus",
            ip="192.168.1.100",
            metadata={"_test_simulated_data": {"P1": 100, "P2": 200}},
        )
        point1 = create_point(device=device, code="P1", address="D100")
        point2 = create_point(device=device, code="P2", address="D101")

        task = create_task(points=[point1, point2])
        session = create_session(task=task)

        service = AcquisitionService(task, session)
        result = service.acquire_once()

        assert result["status"] == "completed"
        assert result["points_read"] == 2
        assert result["errors"] == []
        # The simulated values come back through unchanged.
        values = {row["code"]: row["value"] for row in result["data"]}
        assert values == {"P1": 100, "P2": 200}

    def test_acquire_once_with_error(
        self,
        create_task,
        create_session,
        create_device,
        create_point,
    ):
        """Protocol-level failure surfaces in ``result['errors']`` not raises."""
        device = create_device(
            protocol="mock_modbus",
            metadata={"_test_connection_fail": True},
        )
        point = create_point(device=device, code="P1")

        task = create_task(points=[point])
        session = create_session(task=task)

        service = AcquisitionService(task, session)
        result = service.acquire_once()

        # Mock protocol returns False (no raise) on connect; subsequent
        # read_points raises ``Not connected to device`` -> caught by service.
        assert result["points_read"] == 0
        assert len(result["errors"]) == 1
        assert result["errors"][0]["device"] == device.code

    def test_should_continue_returns_true_while_running(
        self, create_task, create_session,
    ):
        """``_should_continue`` is True iff session.status == STATUS_RUNNING."""
        task = create_task()
        session = create_session(task=task, status=acq_models.AcquisitionSession.STATUS_RUNNING)

        service = AcquisitionService(task, session)

        assert service._should_continue() is True

    def test_should_continue_returns_false_when_stopped(
        self, create_task, create_session,
    ):
        """A stopped session must not keep the main loop alive."""
        task = create_task()
        session = create_session(task=task, status=acq_models.AcquisitionSession.STATUS_RUNNING)

        service = AcquisitionService(task, session)
        # Flip status in the DB; ``_should_continue`` does its own
        # ``refresh_from_db`` so the change is observed.
        session.status = acq_models.AcquisitionSession.STATUS_STOPPED
        session.save(update_fields=["status"])

        assert service._should_continue() is False


@pytest.mark.skip(
    reason=(
        "Cycle-interval calculation moved from AcquisitionService."
        "_get_cycle_interval to ReadWorker.cycle_interval (1.0 / sample_rate_hz). "
        "Worker behaviour is covered by tests/test_pipeline_watchdog.py and "
        "tests/test_read_plan.py."
    )
)
def test_get_cycle_interval():
    pass


# ---------------------------------------------------------------------------
# InfluxDBSink — replaces the old service._format_for_storage / _write_to_storage
# ---------------------------------------------------------------------------


def _make_sink_skeleton(session, device_groups):
    """Build an InfluxDBSink without going through ``_init_storage``.

    The sink's __init__ tries to connect to InfluxDB, which is unwanted in a
    unit test. We bypass it via ``__new__`` and wire up just the state the
    methods under test rely on. This mirrors the pattern used in
    ``tests/test_pipeline_watchdog.py:TestTotalPointsReadPlumbing``.
    """
    import threading
    import time as _time

    sink = InfluxDBSink.__new__(InfluxDBSink)
    sink.session = session
    sink.device_groups = device_groups
    sink._lock = threading.Lock()
    sink._buffer = []
    # Use the current wall clock so the 5-s batch-timeout heuristic in
    # ``consume`` doesn't fire on the very first reading we stage.
    sink._last_flush = _time.time()
    sink._storage = MagicMock()
    sink._storage.write.return_value = True
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


@pytest.mark.django_db
class TestInfluxDBSinkFormatting:
    """The reading -> InfluxDB-point shape that used to be built by
    ``AcquisitionService._format_for_storage``."""

    def test_consume_builds_expected_point_shape(
        self,
        create_task,
        create_session,
        create_device,
        create_point,
        create_point_template,
    ):
        # M5 schema: point_code / cn_name / unit are fields, not tags.
        template = create_point_template(name="temperature", unit="°C")
        device = create_device(code="TEST_DEV", metadata={"device_a_tag": "SENSOR_001"})
        point = create_point(device=device, code="TEMP_01", template=template)

        task = create_task(points=[point])
        session = create_session(task=task)
        service = AcquisitionService(task, session)

        sink = _make_sink_skeleton(session, service.device_groups)

        reading = Reading(
            point_code="TEMP_01",
            value=25.5,
            timestamp_ns=1234567890000000000,
            quality="good",
        )
        sink.consume(reading)

        # consume() always buffers — flush threshold (50 points / 5 s) is
        # not yet hit, so we read the staged point directly.
        assert len(sink._buffer) == 1
        formatted = sink._buffer[0]
        assert formatted["measurement"] == "SENSOR_001"
        # M5: only low-cardinality dimensions are tags.
        assert formatted["tags"]["device"] == "TEST_DEV"
        assert set(formatted["tags"]) <= {"site", "device", "quality"}
        assert "point" not in formatted["tags"]
        assert "cn_name" not in formatted["tags"]
        assert "unit" not in formatted["tags"]
        # point_code is the field key; cn_name / unit are string fields.
        assert formatted["fields"]["TEMP_01"] == 25.5
        assert formatted["fields"]["cn_name"] == template.name
        assert formatted["fields"]["unit"] == "°C"
        assert formatted["time"] == 1234567890000000000

    def test_consume_drops_bad_quality_readings(
        self,
        create_task,
        create_session,
        create_device,
        create_point,
    ):
        """Quality-gated drop: bad readings never reach the buffer."""
        device = create_device(code="DEV_BAD")
        point = create_point(device=device, code="P1")
        task = create_task(points=[point])
        session = create_session(task=task)
        service = AcquisitionService(task, session)

        sink = _make_sink_skeleton(session, service.device_groups)
        sink.consume(
            Reading(point_code="P1", value=42, timestamp_ns=1, quality="bad")
        )

        assert sink._buffer == []


@pytest.mark.django_db
class TestInfluxDBSinkFlush:
    """``flush()`` is what the old service._write_to_storage delegated to."""

    def test_flush_writes_buffered_points_to_storage(
        self, create_task, create_session,
    ):
        task = create_task()
        session = create_session(task=task)
        service = AcquisitionService(task, session)

        sink = _make_sink_skeleton(session, service.device_groups)
        # Stage a single point and flush directly.
        staged = [{
            "measurement": "test",
            "tags": {"site": "test"},
            "fields": {"value": 1},
            "time": 1,
        }]
        sink._buffer.extend(staged)

        sink.flush()

        sink._storage.write.assert_called_once_with(staged)
        # Successful flush drains the buffer and updates total_written.
        assert sink._buffer == []
        assert sink.total_written == 1

    def test_flush_retains_buffer_on_storage_error(
        self, create_task, create_session,
    ):
        """A failing write keeps the batch in the buffer for retry."""
        task = create_task()
        session = create_session(task=task)
        service = AcquisitionService(task, session)

        sink = _make_sink_skeleton(session, service.device_groups)
        sink._storage.write.side_effect = Exception("network down")
        sink._buffer.append({
            "measurement": "x", "tags": {}, "fields": {"v": 1}, "time": 1,
        })

        sink.flush()

        # Buffer untouched, fail counter advanced, total_written stays 0.
        assert len(sink._buffer) == 1
        assert sink._fail_count == 1
        assert sink.total_written == 0


@pytest.mark.django_db
class TestInfluxDBSinkInitialization:
    """Replacement for the old ``TestStorageInitialization`` class.

    The storage handle is now owned by InfluxDBSink, not AcquisitionService.
    We verify the same two paths: success (storage created and connected)
    and failure (init raises -> sink stores None and the pipeline carries
    on without a storage).
    """

    def test_init_storage_success_returns_connected_handle(
        self, create_task, create_session,
    ):
        task = create_task()
        session = create_session(task=task)
        service = AcquisitionService(task, session)

        mock_storage = MagicMock()
        mock_storage.connect.return_value = True

        with patch("storage.StorageRegistry.create", return_value=mock_storage) as create:
            sink = InfluxDBSink(session, service.device_groups)

        create.assert_called_once()
        # First positional argument is the registered name "influxdb".
        assert create.call_args.args[0] == "influxdb"
        assert sink._storage is mock_storage
        mock_storage.connect.assert_called_once()

    def test_init_storage_failure_swallowed_returns_none(
        self, create_task, create_session,
    ):
        task = create_task()
        session = create_session(task=task)
        service = AcquisitionService(task, session)

        with patch(
            "storage.StorageRegistry.create",
            side_effect=Exception("Connection failed"),
        ):
            # Must not raise — pipeline must keep running without InfluxDB.
            sink = InfluxDBSink(session, service.device_groups)

        assert sink._storage is None
