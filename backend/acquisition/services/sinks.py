"""Pluggable sinks consumed by the acquisition pipeline.

Each :class:`Sink` receives :class:`Reading` objects from one or more
``ReadWorker`` threads. Sinks must be **thread-safe** because multiple workers
may call :meth:`Sink.consume` concurrently.

The three sinks implemented here cover the legacy responsibilities of the
old monolithic ``run_continuous`` loop:

* :class:`InfluxDBSink` — buffer + batch-write to time-series storage
* :class:`AlarmSink` — synchronous threshold evaluation
* :class:`WebSocketSink` — 1 Hz aggregated push to Channels groups
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from django.conf import settings

from .read_plan import Reading

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class Sink(ABC):
    """Abstract sink — implementations consume :class:`Reading` instances.

    Lifecycle: ``consume`` is called many times concurrently;
    ``flush`` is invoked at batch boundaries (or stop); ``close`` releases
    resources at pipeline shutdown.
    """

    @abstractmethod
    def consume(self, reading: Reading) -> None:
        """Accept a single reading. MUST be thread-safe."""
        ...

    def flush(self) -> None:  # pragma: no cover - default no-op
        """Force pending data out (called on stop)."""
        return None

    def close(self) -> None:  # pragma: no cover - default no-op
        """Release sink-owned resources."""
        return None


# ---------------------------------------------------------------------------
# InfluxDBSink
# ---------------------------------------------------------------------------


# Match the legacy thresholds so write rate is comparable.
_INFLUX_BATCH_SIZE = 50
_INFLUX_BATCH_TIMEOUT = 5.0  # seconds

# Bounded-buffer + retry tuning. The buffer caps memory at roughly
# 5000 points (~50 s of headroom for a 9-point device polled at 10 Hz);
# beyond that we drop the *oldest* point to preserve the most recent
# samples. Write failures keep the batch in the buffer and impose an
# exponential backoff so we don't hammer a sick InfluxDB.
_MAX_BUFFER_POINTS = 5000
_FLUSH_RETRY_DELAY_S = 1.0
_FLUSH_BACKOFF_MAX_S = 30.0
# Drop-log throttle: emit one warning every N drops to avoid log spam.
_BUFFER_DROP_LOG_EVERY = 100
# Storage (re)connect backoff. When InfluxDB is unreachable at sink
# construction, ``_init_storage`` returns None and the durable spill queue
# (which lives *inside* the storage object) never comes into being. Without a
# retry the whole session would buffer-and-drop forever, even after InfluxDB
# recovers. So flush()/consume() re-attempt the connect on this exponential
# cadence; once it succeeds the buffered points drain normally.
_STORAGE_REINIT_DELAY_S = 1.0
_STORAGE_REINIT_BACKOFF_MAX_S = 30.0


class InfluxDBSink(Sink):
    """Buffer readings and flush to InfluxDB in batches of 50 / 5 s.

    On write failure the batch is *retained* in the buffer and retried
    with exponential backoff (capped at ``_FLUSH_BACKOFF_MAX_S``). The
    buffer itself is bounded at ``_MAX_BUFFER_POINTS``: when full we drop
    the oldest point to keep memory usage flat during long outages.
    """

    def __init__(self, session, device_groups: Dict[int, Dict[str, Any]]) -> None:
        self.session = session
        self.device_groups = device_groups
        self._lock = threading.Lock()
        self._buffer: List[Dict[str, Any]] = []
        self._last_flush = time.time()
        self._storage = self._init_storage()
        # Cumulative count of points successfully written to InfluxDB.
        # Read by AcquisitionPipeline.total_points_read; surfaces in the
        # session metadata so the API can report acquisition progress.
        # Only incremented after a successful flush() — failures do not
        # advance the counter.
        self._total_written = 0
        # Failure-handling state: consecutive failures gate the next
        # allowed flush attempt; dropped_total counts points evicted
        # because the buffer was full. All of these are mutated only while
        # holding self._lock so concurrent flush() calls stay consistent.
        self._fail_count = 0
        self._next_flush_at = 0.0
        self._dropped_total = 0
        # Guards against two worker threads running flush() concurrently —
        # without it both could snapshot the same batch, double-write it,
        # and then delete twice as many points from the buffer.
        self._flush_in_progress = False
        # Storage re-init state (Bug: InfluxDB down at session start). When
        # _init_storage() above failed, self._storage is None; flush()/consume()
        # call _ensure_storage() to retry the connect on a monotonic-clock
        # backoff so a later recovery drains the buffer instead of dropping it.
        # ``_reinit_in_progress`` keeps concurrent workers from piling up
        # overlapping connect() attempts against a sick backend.
        self._storage_reinit_next_at = 0.0
        self._storage_reinit_attempts = 0
        self._storage_reinit_in_progress = False

        # Build lookup maps keyed by point_code so consume() does not touch
        # the ORM. `device_by_point_code` is needed because point_code is
        # unique within a device but not globally — the worker-of-origin
        # determines which device a reading belongs to. We accept the
        # ambiguity and use the *first* device that owns a code; in practice
        # codes are globally unique within a task because the importer
        # de-dups.
        self._point_meta: Dict[str, Dict[str, Any]] = {}
        for _device_id, group in device_groups.items():
            device = group["device"]
            for point in group["points"]:
                self._point_meta[point["code"]] = {
                    "device": device,
                    "coefficient": float(point.get("coefficient", 1.0) or 1.0),
                    "precision": int(point.get("precision", 2) or 2),
                    "template_name": point.get("cn_name") or point.get("description") or "",
                    "template_unit": point.get("unit", "") or "",
                }

    def _init_storage(self):
        from storage import StorageRegistry

        cfg = {
            "url": getattr(settings, "INFLUXDB_URL", None),
            "host": getattr(settings, "INFLUXDB_HOST", "localhost"),
            "port": getattr(settings, "INFLUXDB_PORT", 8086),
            "token": getattr(settings, "INFLUXDB_TOKEN", ""),
            "org": getattr(settings, "INFLUXDB_ORG", "default"),
            "bucket": getattr(settings, "INFLUXDB_BUCKET", "default"),
            "docker_mode": False,
        }
        try:
            storage = StorageRegistry.create("influxdb", cfg)
            storage.connect()
            logger.info("InfluxDBSink: storage connected")
            return storage
        except Exception as exc:  # noqa: BLE001
            logger.warning("InfluxDBSink: failed to init storage: %s", exc)
            return None

    def _ensure_storage(self):
        """Return live storage, retrying a failed startup connect on backoff.

        No-op fast-path when storage already exists. Otherwise at most one
        thread at a time re-attempts ``_init_storage`` once the monotonic-clock
        backoff gate has elapsed; on success ``self._storage`` is populated so
        the next ``flush()`` drains the buffer that accumulated while InfluxDB
        was down. The connect attempt runs *outside* ``self._lock`` because it
        may block.
        """
        if self._storage is not None:
            return self._storage

        now = time.monotonic()
        with self._lock:
            if self._storage is not None:
                return self._storage
            if self._storage_reinit_in_progress:
                return None
            if now < self._storage_reinit_next_at:
                return None
            self._storage_reinit_in_progress = True

        storage = self._init_storage()

        with self._lock:
            self._storage_reinit_in_progress = False
            if storage is not None:
                self._storage = storage
                attempts = self._storage_reinit_attempts
                self._storage_reinit_attempts = 0
                self._storage_reinit_next_at = 0.0
                logger.info(
                    "InfluxDBSink: storage (re)connected after %d failed "
                    "attempt(s); %d buffered point(s) will drain",
                    attempts,
                    len(self._buffer),
                )
            else:
                self._storage_reinit_attempts += 1
                backoff = min(
                    _STORAGE_REINIT_BACKOFF_MAX_S,
                    _STORAGE_REINIT_DELAY_S * (2 ** (self._storage_reinit_attempts - 1)),
                )
                self._storage_reinit_next_at = now + backoff
        return self._storage

    # --------------------------------------------------------------- Sink API
    def consume(self, reading: Reading) -> None:
        if reading.quality != "good":
            return
        meta = self._point_meta.get(reading.point_code)
        if not meta:
            return

        device = meta["device"]
        scaled = self._scale(reading.value, meta["coefficient"], meta["precision"])
        if scaled is None:
            return

        device_metadata = device.metadata or {}
        measurement = device_metadata.get("device_a_tag", device.code) or device.code
        # M5 - cardinality control. Only low-cardinality dimensions belong in
        # tags: the InfluxDB series index is the *product* of tag value counts.
        # point_code is high-cardinality, so it is encoded as the field *key*
        # instead of a tag. cn_name / unit are high-cardinality metadata that
        # move to string fields - still queryable, but they no longer multiply
        # the series index. quality is bounded (good/bad/uncertain).
        tags = {
            "site": device.site.code,
            "device": device.code,
            "quality": reading.quality,
        }
        fields: Dict[str, Any] = {reading.point_code: scaled}
        if meta.get("template_name"):
            fields["cn_name"] = meta["template_name"]
        if meta.get("template_unit"):
            fields["unit"] = meta["template_unit"]

        point = {
            "measurement": measurement,
            "tags": tags,
            "fields": fields,
            "time": reading.timestamp_ns,
        }

        flush_now = False
        with self._lock:
            # Enforce hard upper bound: when the writer is stuck we drop
            # the *oldest* point to preserve recent data. pop(0) is O(n)
            # on a list, but at _MAX_BUFFER_POINTS=5000 it stays sub-ms
            # and only fires while we're already in a degraded state.
            if len(self._buffer) >= _MAX_BUFFER_POINTS:
                self._buffer.pop(0)
                self._dropped_total += 1
                if self._dropped_total % _BUFFER_DROP_LOG_EVERY == 1:
                    logger.warning(
                        "InfluxDBSink buffer full (size=%d), dropped oldest point. "
                        "total_dropped=%d",
                        len(self._buffer),
                        self._dropped_total,
                    )
            self._buffer.append(point)
            if (
                len(self._buffer) >= _INFLUX_BATCH_SIZE
                or (time.time() - self._last_flush) >= _INFLUX_BATCH_TIMEOUT
            ):
                flush_now = True

        if flush_now:
            self.flush()

    def flush(self) -> None:
        now = time.time()

        # If storage never came up (InfluxDB down at session start), retry the
        # connect here — gated by its own backoff — so the buffer that
        # consume() has been accumulating drains once the backend recovers,
        # instead of being silently dropped for the whole session.
        if self._storage is None:
            self._ensure_storage()

        # The backoff gate, the in-progress guard and the buffer snapshot must
        # all be decided under a single lock acquisition. Reading
        # self._next_flush_at outside the lock (as before) raced with the
        # except-branch that sets it: two threads could both pass the gate, or
        # one could see a half-updated value while another was writing it.
        with self._lock:
            # Honour exponential backoff after a recent failure — bail early
            # so we don't pound a sick storage backend on every consume()
            # that crosses the batch threshold.
            if now < self._next_flush_at:
                return
            # Only one flush at a time: a second concurrent caller would
            # snapshot and re-write the same batch.
            if self._flush_in_progress:
                return
            if not self._buffer:
                self._last_flush = now
                return
            if not self._storage:
                return
            # Snapshot the current buffer but DO NOT clear it yet — we
            # only delete the prefix after the write succeeds, so a
            # failure leaves the data intact for the next retry.
            batch = list(self._buffer)
            self._flush_in_progress = True

        try:
            self._storage.write(batch)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._fail_count += 1
                fail_count = self._fail_count
                backoff = min(
                    _FLUSH_BACKOFF_MAX_S,
                    _FLUSH_RETRY_DELAY_S * (2 ** (self._fail_count - 1)),
                )
                self._next_flush_at = now + backoff
                self._flush_in_progress = False
            logger.warning(
                "InfluxDBSink flush failed (#%d, retry in %.1fs, buffer=%d): %s",
                fail_count,
                backoff,
                len(batch),
                exc,
            )
            return

        # Write succeeded — remove exactly the points we wrote. Other
        # consume() calls may have appended new points concurrently;
        # those stay in the buffer for the next flush.
        with self._lock:
            del self._buffer[: len(batch)]
            self._last_flush = now
            recovered_from = self._fail_count
            self._fail_count = 0
            self._next_flush_at = 0.0
            # Only count points that actually made it to storage.
            self._total_written += len(batch)
            self._flush_in_progress = False
        if recovered_from:
            logger.info(
                "InfluxDBSink flush recovered after %d failure(s), wrote %d point(s)",
                recovered_from,
                len(batch),
            )

    @property
    def total_written(self) -> int:
        """Cumulative successful-write count since sink construction."""
        return self._total_written

    def close(self) -> None:
        try:
            self.flush()
        finally:
            if self._storage:
                try:
                    self._storage.disconnect()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("InfluxDBSink disconnect failed: %s", exc)

    # ------------------------------------------------------------- helpers
    @staticmethod
    def _scale(raw: Any, coefficient: float, precision: int) -> Any:
        if raw is None:
            return None
        if isinstance(raw, bool):
            # bool readings are forwarded as-is (no scaling)
            return raw
        if isinstance(raw, (int, float)):
            value = float(raw) * coefficient if coefficient != 1.0 else raw
            if isinstance(value, float):
                try:
                    value = round(value, int(precision))
                except (TypeError, ValueError):
                    pass
            return value
        return raw

    def write_health_points(self, health_points: List[Dict[str, Any]]) -> None:
        """Bypass the buffer and write health metrics directly.

        The pipeline owns health observation (it sees the workers); the sink
        just provides a write channel. Called from the AcquisitionService
        main loop, never from a worker thread.
        """
        if not self._storage or not health_points:
            return
        try:
            self._storage.write(health_points)
        except Exception as exc:  # noqa: BLE001
            logger.warning("InfluxDBSink health write failed: %s", exc)


# ---------------------------------------------------------------------------
# AlarmSink
# ---------------------------------------------------------------------------


# Sentinel pushed onto the AlarmSink queue to tell the writer thread to exit.
_ALARM_SENTINEL = object()
# Hard cap on the AlarmSink internal queue. At 20 Hz x N devices the writer
# is the bottleneck, so this is sized for a few seconds of buffering before
# we start dropping (preferring backpressure to unbounded memory growth).
_ALARM_QUEUE_MAXSIZE = 1000
# Drop-log throttle: emit one warning every N drops to avoid log spam.
_ALARM_DROP_LOG_EVERY = 100


class AlarmSink(Sink):
    """Evaluate readings against active alarm rules — asynchronously.

    Readings are queued on a bounded ``queue.Queue`` and drained by a single
    daemon writer thread that calls :func:`evaluate_readings`. This isolates
    the worker-thread hot path (which may be running at 20 Hz) from the 5-10
    ms SQLite write + Channels group_send that alarm evaluation triggers on
    a hit.

    The shared ``evaluate_readings`` helper expects the legacy reading dict
    shape (``{"code", "value", "quality"}``), so we adapt :class:`Reading`
    on the fly. We also need to scale the value to engineering units before
    comparing against thresholds (rules are authored in physical units).
    """

    def __init__(self, session, device_groups: Optional[Dict[int, Dict[str, Any]]] = None) -> None:
        self.session = session
        self._point_meta: Dict[str, Dict[str, Any]] = {}
        if device_groups:
            for _device_id, group in device_groups.items():
                device = group["device"]
                for point in group["points"]:
                    self._point_meta[point["code"]] = {
                        "device_code": device.code,
                        "coefficient": float(point.get("coefficient", 1.0) or 1.0),
                        "precision": int(point.get("precision", 2) or 2),
                    }

        self._queue: "queue.Queue[Any]" = queue.Queue(maxsize=_ALARM_QUEUE_MAXSIZE)
        self._closed = threading.Event()
        self._dropped = 0
        self._writer = threading.Thread(
            target=self._writer_loop,
            daemon=True,
            name=f"AlarmSink-{getattr(session, 'id', '?')}",
        )
        self._writer.start()
        logger.info(
            "AlarmSink writer started: session=%s queue_maxsize=%d",
            getattr(session, "id", "?"),
            _ALARM_QUEUE_MAXSIZE,
        )

    # --------------------------------------------------------------- producer
    def consume(self, reading: Reading) -> None:
        # After close() we must not enqueue further work — the writer thread
        # has already received its sentinel and may have exited. Dropping
        # silently here avoids a race where flush() returns and a late
        # consume() resurrects work for a dead consumer.
        if self._closed.is_set():
            return
        if reading.quality != "good":
            return
        meta = self._point_meta.get(reading.point_code)
        if not meta:
            return

        try:
            self._queue.put_nowait((reading, meta))
        except queue.Full:
            self._dropped += 1
            if self._dropped % _ALARM_DROP_LOG_EVERY == 1:
                logger.warning(
                    "AlarmSink queue full — dropped %d reading(s) so far "
                    "(queue_maxsize=%d). Alarm evaluation cannot keep up "
                    "with the read rate.",
                    self._dropped, _ALARM_QUEUE_MAXSIZE,
                )

    # --------------------------------------------------------------- consumer
    def _writer_loop(self) -> None:
        """Drain the queue serially. Order is preserved (FIFO Queue)."""
        while True:
            item = self._queue.get()
            # task_done() MUST run for every get() — including the sentinel
            # path and any exception raised by _evaluate_one. If it were
            # skipped on an error, unfinished_tasks would never reach 0 and
            # flush() (which polls unfinished_tasks) would block for its full
            # 5 s timeout on every batch boundary. Hence the finally block.
            try:
                if item is _ALARM_SENTINEL:
                    return
                reading, meta = item
                self._evaluate_one(reading, meta)
            except Exception as exc:  # noqa: BLE001
                logger.warning("AlarmSink writer iteration failed: %s", exc)
            finally:
                try:
                    self._queue.task_done()
                except ValueError:
                    pass

    def _evaluate_one(self, reading: Reading, meta: Dict[str, Any]) -> None:
        scaled = InfluxDBSink._scale(reading.value, meta["coefficient"], meta["precision"])
        adapter = [{
            "code": reading.point_code,
            "value": scaled,
            "quality": reading.quality,
        }]
        try:
            from .alarms import evaluate_readings

            evaluate_readings(self.session, meta["device_code"], adapter)
        except Exception as exc:  # noqa: BLE001
            logger.warning("AlarmSink evaluate failed: %s", exc)
        finally:
            # ORM writes inside the writer thread need an explicit
            # connection close to avoid leaking SQLite handles — same
            # pattern ReadWorker uses.
            try:
                from django.db import connection

                connection.close()
            except Exception:  # noqa: BLE001
                pass

    # --------------------------------------------------------------- lifecycle
    def flush(self) -> None:
        """Wait (up to 5 s) for the writer to drain the queue."""
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if self._queue.unfinished_tasks == 0:
                return
            time.sleep(0.05)
        logger.warning(
            "AlarmSink flush timeout: %d items still pending after 5s",
            self._queue.unfinished_tasks,
        )

    def close(self) -> None:
        # Mark closed *before* enqueueing the sentinel so any concurrent
        # consume() call sees the flag and bails out cleanly.
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._queue.put(_ALARM_SENTINEL, timeout=1.0)
        except queue.Full:
            # Queue is jammed — drain forcibly so the sentinel can land.
            try:
                while True:
                    self._queue.get_nowait()
                    self._queue.task_done()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(_ALARM_SENTINEL)
            except queue.Full:
                pass
        self._writer.join(timeout=5.0)
        if self._dropped:
            logger.info(
                "AlarmSink closed: total dropped readings=%d", self._dropped,
            )


# ---------------------------------------------------------------------------
# WebSocketSink
# ---------------------------------------------------------------------------


# Hard cap on the number of readings packed into a single WS frame. A
# session with thousands of points would otherwise build one oversized
# message that the channels-redis layer can silently drop (it enforces a
# per-message size limit and a bounded channel ``capacity``). When a drain
# exceeds this we split it across several frames instead.
_WS_MAX_READINGS_PER_MSG = 200


class WebSocketSink(Sink):
    """Aggregate readings and broadcast on a fixed cadence (default 1 Hz).

    Flushing happens on a background timer thread, never inside
    :meth:`consume`. ``group_send`` is comparatively expensive, so emitting
    once per second per session keeps overhead bounded even at high sample
    rates.
    """

    def __init__(self, session, broadcast_interval: float = 1.0) -> None:
        self.session = session
        self.broadcast_interval = max(0.05, float(broadcast_interval))
        self._lock = threading.Lock()
        self._buffer: List[Reading] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._broadcast_loop,
            daemon=True,
            name=f"WebSocketSink-{session.id}",
        )
        self._thread.start()

        # Resolve channel layer once. None means Channels is unavailable
        # (tests, lightweight runs); consume() then short-circuits.
        try:
            from channels.layers import get_channel_layer

            self._channel_layer = get_channel_layer()
        except Exception:  # noqa: BLE001
            self._channel_layer = None

    def consume(self, reading: Reading) -> None:
        with self._lock:
            self._buffer.append(reading)

    def consume_event(self, event: Dict[str, Any]) -> None:
        """Broadcast a connection-lifecycle event immediately.

        Bypasses the 1 Hz aggregation buffer so the operator sees the
        state change as soon as the worker emits it. ``event`` is merged
        into the outgoing payload alongside ``session_id`` and
        ``timestamp``.

        Safe to call from any worker thread — ``async_to_sync`` handles
        the cross-thread group_send for us.
        """
        if not self._channel_layer:
            return

        payload = {
            "session_id": self.session.id,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            **event,
        }
        envelope = {
            "type": "connection_event",
            "data": payload,
        }

        from asgiref.sync import async_to_sync

        for group in (
            f"acquisition_session_{self.session.id}",
            "acquisition_global",
        ):
            try:
                async_to_sync(self._channel_layer.group_send)(group, envelope)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "WebSocketSink connection_event group_send to %s failed: %s",
                    group,
                    exc,
                )

    def _drain(self) -> List[Reading]:
        with self._lock:
            if not self._buffer:
                return []
            batch = self._buffer
            self._buffer = []
            return batch

    def _broadcast_loop(self) -> None:
        # Compensated cadence: a naive ``wait(interval)`` then broadcast
        # makes the real period ``interval + broadcast_cost``, so the push
        # rate drifts slower than configured (and drifts further the more
        # expensive ``group_send`` gets). Subtract the work time from the
        # next wait so the broadcast lands on a fixed cadence.
        next_wait = self.broadcast_interval
        while not self._stop.is_set():
            if self._stop.wait(next_wait):
                break
            cycle_start = time.monotonic()
            try:
                self._broadcast_once()
            except Exception as exc:  # noqa: BLE001
                logger.warning("WebSocketSink broadcast failed: %s", exc)
            elapsed = time.monotonic() - cycle_start
            next_wait = max(0.0, self.broadcast_interval - elapsed)

    def _broadcast_once(self) -> None:
        batch = self._drain()
        if not batch or not self._channel_layer:
            return
        # Latest value wins per point — keeps the WS payload bounded
        latest: Dict[str, Reading] = {}
        for r in batch:
            latest[r.point_code] = r

        readings_payload = []
        for r in latest.values():
            ts = datetime.fromtimestamp(r.timestamp_ns / 1e9, tz=timezone.utc).isoformat()
            readings_payload.append({
                "point_code": r.point_code,
                "value": r.value,
                "quality": r.quality,
                "timestamp": ts,
            })

        from asgiref.sync import async_to_sync

        # Split oversized drains across several frames so no single
        # group_send payload exceeds what the channel layer will carry.
        chunks = [
            readings_payload[i : i + _WS_MAX_READINGS_PER_MSG]
            for i in range(0, len(readings_payload), _WS_MAX_READINGS_PER_MSG)
        ]
        total_chunks = len(chunks)
        for idx, chunk in enumerate(chunks):
            envelope = {
                "type": "data_point_update",
                "data": {
                    "session_id": self.session.id,
                    "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                    "readings": chunk,
                    # Frame index hints let the client reassemble a drain
                    # that had to be split; single-frame pushes still carry
                    # them (0 / 1) so the shape is uniform.
                    "chunk_index": idx,
                    "chunk_count": total_chunks,
                },
            }
            for group in (f"acquisition_session_{self.session.id}", "acquisition_global"):
                try:
                    async_to_sync(self._channel_layer.group_send)(group, envelope)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("WebSocketSink group_send to %s failed: %s", group, exc)

    def flush(self) -> None:
        # Drain anything left in the buffer.
        try:
            self._broadcast_once()
        except Exception as exc:  # noqa: BLE001
            logger.warning("WebSocketSink flush broadcast failed: %s", exc)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        # Final drain in case readings arrived between last broadcast and close.
        try:
            self._broadcast_once()
        except Exception:  # noqa: BLE001
            pass
