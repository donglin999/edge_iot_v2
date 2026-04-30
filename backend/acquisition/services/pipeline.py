"""Producer / consumer pipeline for continuous acquisition.

Each device gets a dedicated :class:`ReadWorker` thread that owns its
protocol connection and produces :class:`Reading` instances at the
configured ``sample_rate_hz``. Readings are fanned out to a list of
:class:`Sink` instances (InfluxDB / Alarm / WebSocket) shared across all
workers.

Why threads, not asyncio?
    - The protocol stack (modbus-tk, snap7, ...) is sync-only.
    - Each device is a single TCP connection — no benefit from sharing an
      event loop.
    - Sinks include stdlib I/O (InfluxDB HTTP) which is fine to do from a
      worker thread.

The pipeline never touches Django ORM from inside a worker (apart from
read-only attribute access on the device passed at construction). All ORM
writes happen either in the main loop (``AcquisitionService``) or inside a
sink that owns the responsibility (``AlarmSink``).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from acquisition.protocols import ProtocolRegistry

from .read_plan import ReadPlanBuilder, Reading
from .sinks import AlarmSink, InfluxDBSink, Sink, WebSocketSink

logger = logging.getLogger(__name__)


# Aggregate read_failed events: emit either every N failures or every
# WINDOW seconds, whichever comes first. Keeps the "operator log" panel
# from being flooded when a device is hard-down (which may produce a
# failure on every cycle).
_READ_FAILED_BURST = 10
_READ_FAILED_WINDOW_S = 5.0


# ---------------------------------------------------------------------------
# ReadWorker
# ---------------------------------------------------------------------------


class ReadWorker(threading.Thread):
    """One device, one thread, one persistent protocol connection."""

    def __init__(
        self,
        device,
        points: List[Dict[str, Any]],
        sinks: List[Sink],
        *,
        sample_rate_hz: float,
        shutdown_event: threading.Event,
        health_dict: Dict[int, Dict[str, Any]],
        max_reconnect: int = 3,
        connection_timeout: float = 30.0,
        reconnect_backoff: float = 5.0,
    ) -> None:
        super().__init__(daemon=True, name=f"ReadWorker-{device.code}")
        self.device = device
        self.points = points
        self.sinks = sinks
        self.read_groups = ReadPlanBuilder.build(device, _PointAdapter.iter(points))
        self.cycle_interval = 1.0 / max(0.0001, float(sample_rate_hz))
        self.shutdown_event = shutdown_event
        self.max_reconnect = int(max_reconnect)
        self.connection_timeout = float(connection_timeout)
        self.reconnect_backoff = float(reconnect_backoff)
        self.protocol = None
        self.health = health_dict.setdefault(
            device.id,
            {
                "status": "init",
                "consecutive_failures": 0,
                "last_success": None,
                "device_code": device.code,
            },
        )

        # ----- lifecycle event state machine ---------------------------
        # Locate the WebSocketSink once; if absent (tests / lightweight
        # runs) ``_emit`` becomes a no-op.
        self._ws_sink: Optional[WebSocketSink] = next(
            (s for s in sinks if isinstance(s, WebSocketSink)), None,
        )
        # Has this worker ever been connected? Drives the
        # connected-vs-reconnected emission decision.
        self._has_connected_once = False
        # Set when we transition into a disconnected state; cleared by the
        # next successful connect. Used to compute downtime_seconds.
        self._disconnected_at: Optional[float] = None
        # Has the "connecting" event already been emitted for the current
        # disconnected->connecting cycle? Prevents duplicate emissions on
        # tick-by-tick retries.
        self._connecting_emitted = False
        # Per-disconnect-cycle counter — incremented on each retry attempt.
        self._reconnect_attempt = 0
        # Tracks whether ``gave_up`` has already been emitted for the
        # current backoff window so we don't repeat it every loop.
        self._gave_up_emitted = False
        # Aggregation state for read_failed events.
        self._failed_count = 0
        self._failed_window_start: Optional[float] = None
        self._last_error: str = ""

        # Derive a per-cycle protocol timeout once. At 20 Hz the default
        # 5 s timeout swallows ~100 cycles on a single hiccup; tighten it
        # so a stuck I/O fails fast and the next cycle gets a fresh shot.
        metadata = self.device.metadata or {}
        configured_timeout = float(metadata.get("timeout", 5.0))
        self._auto_timeout = max(0.1, min(configured_timeout, self.cycle_interval * 2))
        logger.info(
            "ReadWorker[%s] cycle=%.0fms timeout=%.0fms",
            self.device.code,
            self.cycle_interval * 1000,
            self._auto_timeout * 1000,
        )

    # --------------------------------------------------------------- lifecycle
    def _emit(self, event_type: str, **payload: Any) -> None:
        """Forward a connection-lifecycle event to ``WebSocketSink``.

        Silent no-op when the pipeline was started without a
        ``WebSocketSink`` (tests, headless runs). Any sink-side error is
        swallowed — the read loop must never abort because of telemetry.
        """
        if self._ws_sink is None:
            return
        try:
            self._ws_sink.consume_event({
                "event": event_type,
                "device_code": self.device.code,
                "device_id": self.device.id,
                **payload,
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ReadWorker[%s] emit %s failed: %s",
                self.device.code, event_type, exc,
            )

    def _flush_failed_events(self, *, force: bool = False) -> None:
        """Emit a ``read_failed`` aggregate when the burst threshold or
        window has elapsed (or ``force`` is set, e.g. on disconnect)."""
        if self._failed_count <= 0:
            return
        now = time.time()
        elapsed = now - (self._failed_window_start or now)
        if not force and self._failed_count < _READ_FAILED_BURST and elapsed < _READ_FAILED_WINDOW_S:
            return
        self._emit(
            "read_failed",
            count=self._failed_count,
            last_error=self._last_error,
            window_seconds=round(elapsed, 2),
        )
        self._failed_count = 0
        self._failed_window_start = None
        self._last_error = ""

    def _connect(self) -> bool:
        # Edge: emit ``connecting`` only once per disconnect->connecting
        # cycle, regardless of how many retries the run loop schedules.
        if not self._connecting_emitted:
            self._emit("connecting")
            self._connecting_emitted = True

        connect_start = time.time()
        try:
            metadata = self.device.metadata or {}
            cfg = {
                "source_ip": self.device.ip_address,
                "source_port": self.device.port,
                "protocol_type": self.device.protocol,
                **metadata,
                # IMPORTANT: ``timeout`` must come after ``**metadata``
                # so the auto-derived value overrides any user-supplied
                # value rather than the other way around.
                "timeout": self._auto_timeout,
            }
            self.protocol = ProtocolRegistry.create(self.device.protocol, cfg)
            self.protocol.connect()
            self.health["status"] = "healthy"
            self.health["consecutive_failures"] = 0
            self.health["last_success"] = time.time()
            logger.info("ReadWorker[%s] connected", self.device.code)

            connect_duration_ms = int((time.time() - connect_start) * 1000)

            # Edge: connected (first time) vs reconnected (recovered from a
            # prior disconnected state).
            if self._disconnected_at is not None:
                downtime = int(time.time() - self._disconnected_at)
                self._emit("reconnected", downtime_seconds=downtime)
                self._disconnected_at = None
            elif not self._has_connected_once:
                self._emit(
                    "connected",
                    connect_duration_ms=connect_duration_ms,
                )
            self._has_connected_once = True

            # Reset per-cycle event flags now that we are back online.
            self._connecting_emitted = False
            self._reconnect_attempt = 0
            self._gave_up_emitted = False
            return True
        except Exception as exc:  # noqa: BLE001
            self.health["consecutive_failures"] += 1
            self.health["status"] = "disconnected"
            self.protocol = None
            logger.warning("ReadWorker[%s] connect failed: %s", self.device.code, exc)
            return False

    def run(self) -> None:
        # Initial connect; failure is fine — the loop will retry with backoff.
        self._connect()

        while not self.shutdown_event.is_set():
            cycle_start = time.time()

            if self.protocol is None or not getattr(self.protocol, "is_connected", False):
                # Time-windowed flush of accumulated read_failed events
                # even while disconnected, so a long downtime still shows
                # at least one aggregate entry.
                self._flush_failed_events()

                if self.health["consecutive_failures"] >= self.max_reconnect:
                    if not self._gave_up_emitted:
                        self._emit(
                            "gave_up",
                            will_retry_in_seconds=int(self.reconnect_backoff),
                        )
                        self._gave_up_emitted = True
                    if self.shutdown_event.wait(self.reconnect_backoff):
                        break
                    # Reset so we re-attempt; otherwise we'd never recover.
                    self.health["consecutive_failures"] = 0
                    self._reconnect_attempt = 0
                    self._gave_up_emitted = False
                    # Allow a fresh ``connecting`` event for the next
                    # recovery cycle — without this the UI sees gave_up
                    # silently retrying forever.
                    self._connecting_emitted = False

                # Emit ``reconnecting`` on the *final* attempt before the
                # backoff branch fires — only when we previously had a live
                # connection (avoid spamming during the very first connect).
                if self._has_connected_once:
                    self._reconnect_attempt += 1
                    if self._reconnect_attempt == self.max_reconnect:
                        self._emit(
                            "reconnecting",
                            attempt=self._reconnect_attempt,
                            max_attempts=self.max_reconnect,
                        )
                self._connect()

            if self.protocol and getattr(self.protocol, "is_connected", False):
                self._read_cycle()
                # Window-based flush in the healthy path.
                self._flush_failed_events()

            elapsed = time.time() - cycle_start
            sleep = max(0.0, self.cycle_interval - elapsed)
            if sleep > 0:
                if self.shutdown_event.wait(sleep):
                    break

        # Final flush so partial aggregates still surface in the UI log.
        self._flush_failed_events(force=True)

        # Graceful disconnect
        if self.protocol is not None:
            try:
                self.protocol.disconnect()
            except Exception as exc:  # noqa: BLE001
                logger.warning("ReadWorker[%s] disconnect error: %s", self.device.code, exc)
        logger.info("ReadWorker[%s] stopped", self.device.code)

    # --------------------------------------------------------------- per-cycle
    def _read_cycle(self) -> None:
        for group in self.read_groups:
            if self.shutdown_event.is_set():
                return
            try:
                readings: List[Reading] = self.protocol.read_batch(group)
            except Exception as exc:  # noqa: BLE001
                self._record_failure(exc)
                continue

            self.health["last_success"] = time.time()
            self.health["consecutive_failures"] = 0
            self.health["status"] = "healthy"

            for reading in readings:
                for sink in self.sinks:
                    try:
                        sink.consume(reading)
                    except Exception as exc:  # noqa: BLE001
                        # Sink errors must not interrupt the read flow.
                        logger.warning(
                            "Sink %s.consume failed: %s",
                            sink.__class__.__name__, exc,
                        )

    def _record_failure(self, exc: Exception) -> None:
        self.health["consecutive_failures"] += 1
        last = self.health.get("last_success")
        now = time.time()

        # Aggregate read_failed for the operator log.
        if self._failed_window_start is None:
            self._failed_window_start = now
        self._failed_count += 1
        self._last_error = str(exc)
        self._flush_failed_events()

        if last and (now - last) > self.connection_timeout:
            self.health["status"] = "timeout"
            try:
                if self.protocol:
                    self.protocol.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self.protocol = None
            # Edge: only the *first* timeout per disconnect cycle emits
            # ``disconnected``. Subsequent ticks find ``self.protocol``
            # already None and skip this branch.
            if self._disconnected_at is None:
                self._disconnected_at = now
                self._emit(
                    "disconnected",
                    reason="timeout",
                    after_seconds=int(self.connection_timeout),
                )
                # Force-flush whatever read_failed counts remain so the
                # disconnected event is preceded by an accurate failure
                # tally rather than the next aggregate window.
                self._flush_failed_events(force=True)
        else:
            self.health["status"] = "error"
        logger.warning("ReadWorker[%s] read failed: %s", self.device.code, exc)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class AcquisitionPipeline:
    """Owns the worker pool + sinks for a single :class:`AcquisitionSession`."""

    def __init__(self, session, sample_rate_hz: float) -> None:
        self.session = session
        self.sample_rate_hz = float(sample_rate_hz)
        self.shutdown_event = threading.Event()
        self.health: Dict[int, Dict[str, Any]] = {}
        self.workers: List[ReadWorker] = []
        self.sinks: List[Sink] = []
        # InfluxDBSink is exposed so the service can write health points
        # through its connection without re-opening one.
        self.influx_sink: Optional[InfluxDBSink] = None

    def start(self, device_groups: Dict[int, Dict[str, Any]]) -> None:
        # Sinks are shared by every worker. Construction must happen before
        # any worker thread starts — the worker constructor stores a snapshot
        # of the list.
        influx = InfluxDBSink(self.session, device_groups)
        self.influx_sink = influx
        self.sinks = [
            influx,
            AlarmSink(self.session, device_groups=device_groups),
            WebSocketSink(self.session, broadcast_interval=1.0),
        ]

        for _device_id, group in device_groups.items():
            worker = ReadWorker(
                device=group["device"],
                points=group["points"],
                sinks=self.sinks,
                sample_rate_hz=self.sample_rate_hz,
                shutdown_event=self.shutdown_event,
                health_dict=self.health,
            )
            self.workers.append(worker)
            worker.start()

        logger.info(
            "Pipeline started: session=%s workers=%d sample_rate_hz=%.2f",
            self.session.id, len(self.workers), self.sample_rate_hz,
        )

    def stop(self, timeout: float = 10.0) -> None:
        self.shutdown_event.set()
        for w in self.workers:
            w.join(timeout=timeout)
        for sink in self.sinks:
            try:
                sink.flush()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Sink %s.flush failed: %s", sink.__class__.__name__, exc)
            try:
                sink.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Sink %s.close failed: %s", sink.__class__.__name__, exc)
        logger.info("Pipeline stopped: session=%s", self.session.id)

    def is_alive(self) -> bool:
        return any(w.is_alive() for w in self.workers)

    @property
    def total_points_read(self) -> int:
        """Cumulative count of readings successfully written to InfluxDB.

        Defers to :attr:`InfluxDBSink.total_written`. Returns 0 if the
        pipeline has not been started or was constructed without an
        InfluxDBSink (tests).
        """
        return self.influx_sink.total_written if self.influx_sink else 0

    def dead_workers(self) -> List[ReadWorker]:
        """Return the list of workers whose thread has terminated.

        A worker is considered dead when ``Thread.is_alive()`` is False
        but ``shutdown_event`` has not been set — i.e. the worker died
        on its own rather than being asked to stop. The shutdown_event
        check prevents flagging workers during normal teardown.
        """
        if self.shutdown_event.is_set():
            return []
        return [w for w in self.workers if not w.is_alive()]

    def replace_worker(self, old: ReadWorker, new: ReadWorker) -> None:
        """Swap a dead worker for a freshly constructed one.

        Caller is responsible for constructing ``new`` with the same
        device / sinks / sample_rate as ``old``. The old worker is not
        joined — it is already dead, and joining a never-started new
        thread would block. The new worker is started here.
        """
        try:
            idx = self.workers.index(old)
        except ValueError:
            # Already replaced (or never tracked); start the new one
            # anyway so the device keeps producing data.
            self.workers.append(new)
            new.start()
            return
        self.workers[idx] = new
        new.start()


# ---------------------------------------------------------------------------
# Adapter: dict points -> read_plan-friendly objects
# ---------------------------------------------------------------------------


class _PointAdapter:
    """Wrap a point dict so :class:`ReadPlanBuilder` sees the attributes it
    expects (``code``, ``address``, ``extra``, ``template``)."""

    __slots__ = ("code", "address", "extra", "template")

    def __init__(self, raw: Dict[str, Any]) -> None:
        self.code = raw["code"]
        self.address = raw.get("address", "0")
        # extras: every key in the dict that the protocol layer understands.
        # ReadPlanBuilder pulls function_code / data_type / num from here.
        self.extra = {k: v for k, v in raw.items() if k not in ("code", "address")}
        self.template = None

    @classmethod
    def iter(cls, raw_points):
        return [cls(p) for p in raw_points]
