"""Regression tests for InfluxDBSink storage re-init after a startup outage.

Bug: if InfluxDB is down when the sink is constructed, ``_init_storage`` catches
the error and leaves ``self._storage = None`` — and nothing ever re-attempted
the connect. ``flush()`` short-circuited on ``if not self._storage: return`` and
``consume()`` just buffered to the 5000-point bound then dropped the oldest, so
the *whole session* silently buffered-and-dropped forever, even after InfluxDB
came back. (The durable spill queue lives inside the storage object that was
never created, so it never engaged either.)

Fix: flush()/consume() retry the connect on a bounded monotonic-clock backoff
via ``_ensure_storage``; once InfluxDB recovers the buffered points drain.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from acquisition.services.sinks import InfluxDBSink
from acquisition.services.read_plan import Reading
from tests.fixtures.factories import *  # noqa: F401,F403


def _reading(code: str, value: int) -> Reading:
    return Reading(point_code=code, value=value, timestamp_ns=1_000 + value, quality="good")


@pytest.mark.django_db
class TestStartupDownThenRecovers:
    def test_buffer_drains_once_influx_recovers(
        self, create_task, create_session, create_device, create_point,
    ):
        device = create_device(code="RECOV_DEV")
        point = create_point(device=device, code="P_RECOVER")
        task = create_task(points=[point])
        session = create_session(task=task)

        from acquisition.services.acquisition_service import AcquisitionService
        service = AcquisitionService(task, session)

        mock_storage = MagicMock()
        mock_storage.connect.return_value = True
        mock_storage.write.return_value = True

        # InfluxDB down at construction, healthy on the next create() attempt.
        create_mock = MagicMock(side_effect=[Exception("influx down"), mock_storage])

        with patch("storage.StorageRegistry.create", create_mock):
            sink = InfluxDBSink(session, service.device_groups)
            # Startup failed: no storage handle, but the sink is alive.
            assert sink._storage is None

            # Session runs: readings are buffered (NOT dropped) while down.
            for i in range(3):
                sink.consume(_reading("P_RECOVER", i))
            assert sink._storage is None
            assert len(sink._buffer) == 3

            # InfluxDB recovers. Pretend the backoff window elapsed so the next
            # flush re-attempts the connect immediately.
            sink._storage_reinit_next_at = 0.0
            sink.flush()

        # Storage reconnected and the buffered points drained to it.
        assert sink._storage is mock_storage
        mock_storage.write.assert_called_once()
        written = mock_storage.write.call_args.args[0]
        assert len(written) == 3
        assert sink._buffer == []
        assert sink.total_written == 3

    def test_reinit_is_rate_limited_by_backoff(
        self, create_task, create_session, create_device, create_point,
    ):
        """While InfluxDB stays down, flush() must not spin-retry connect on
        every call — the backoff gate blocks a second immediate attempt."""
        device = create_device(code="BACKOFF_DEV")
        point = create_point(device=device, code="P_BACKOFF")
        task = create_task(points=[point])
        session = create_session(task=task)

        from acquisition.services.acquisition_service import AcquisitionService
        service = AcquisitionService(task, session)

        # create() always fails — InfluxDB never comes back during the test.
        create_mock = MagicMock(side_effect=Exception("still down"))

        with patch("storage.StorageRegistry.create", create_mock):
            sink = InfluxDBSink(session, service.device_groups)  # attempt #1 (__init__)
            assert sink._storage is None

            sink.consume(_reading("P_BACKOFF", 1))

            sink.flush()  # attempt #2 (gate was open: next_at == 0)
            sink.flush()  # gate now closed by backoff -> no new attempt

        # Exactly two connect attempts: construction + one gated flush retry.
        assert create_mock.call_count == 2
        # Data preserved for a future recovery, not dropped.
        assert len(sink._buffer) == 1
