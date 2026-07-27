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

from .device_config import build_device_config
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
        session=None,
    ) -> None:
        super().__init__(daemon=True, name=f"ReadWorker-{device.code}")
        self.device = device
        self.points = points
        self.sinks = sinks
        # Owning acquisition session (for alarm scoping). Optional — when the
        # pipeline runs a worker without one, connectivity alarms are keyed by
        # ``device_code`` alone (``session=None``).
        self._session = session
        self.read_groups = ReadPlanBuilder.build(device, _PointAdapter.iter(points))
        self.cycle_interval = 1.0 / max(0.0001, float(sample_rate_hz))
        self.shutdown_event = shutdown_event
        self.max_reconnect = int(max_reconnect)
        self.connection_timeout = float(connection_timeout)
        self.reconnect_backoff = float(reconnect_backoff)
        self.protocol = None
        # ----- health record (cross-thread) ----------------------------
        # ``health_dict`` is shared with the pipeline's supervising loop,
        # which reads this worker's record from a different thread. We
        # never mutate the live record key-by-key (the reader could then
        # observe a half-updated dict); instead ``_update_health`` swaps a
        # freshly built dict in atomically. Keep a reference to the shared
        # dict + our key so the swap can target it.
        self._health_dict = health_dict
        self._device_id = device.id
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
        # ----- data-loss (queue overflow) tracking ----------------------
        # 推模式协议(MQTT/scada)队列满时静默丢新消息,协议实例只维护自己的
        # dropped_messages;重连会换新实例、计数归零,所以 worker 层折叠出跨
        # 实例的累计值(_dropped_base + 当前实例),并在增长时拉数据丢弃告警、
        # 静默一段时间后自动清除。
        self._dropped_base = 0
        self._last_dropped_seen = 0
        self._last_drop_increase_at: Optional[float] = None
        self._drop_alarm_raised = False
        # Tracks whether ``gave_up`` has already been emitted for the
        # current backoff window so we don't repeat it every loop.
        self._gave_up_emitted = False
        # One-shot guard for the PERSISTED connectivity alarm. Set when the
        # healthy->down transition raises the alarm, cleared on recovery. The
        # ephemeral WS ``gave_up``/``reconnecting`` events flap per backoff
        # cycle; this alarm must fire once and be cleared once, so it needs a
        # flag whose lifetime spans the whole downtime (not per-cycle).
        self._offline_alarm_raised = False
        # Aggregation state for read_failed events.
        self._failed_count = 0
        self._failed_window_start: Optional[float] = None
        self._last_error: str = ""

        # Derive a per-cycle protocol timeout once. At 20 Hz the default
        # 5 s timeout swallows ~100 cycles on a single hiccup; tighten it
        # so a stuck I/O fails fast and the next cycle gets a fresh shot.
        metadata = self.device.metadata or {}
        configured_timeout = float(metadata.get("timeout", 5.0))
        # Floor at 500 ms: anything tighter would abort slow-but-healthy
        # devices (serial RTU, multi-register reads) mid-transaction.
        self._auto_timeout = max(0.5, min(configured_timeout, self.cycle_interval * 2))
        logger.info(
            "ReadWorker[%s] cycle=%.0fms timeout=%.0fms",
            self.device.code,
            self.cycle_interval * 1000,
            self._auto_timeout * 1000,
        )

    # --------------------------------------------------------------- helpers
    def _update_health(self, **changes: Any) -> None:
        """Atomically publish a health update.

        The pipeline's supervising loop reads ``health_dict[device_id]``
        from another thread. Mutating the live record key-by-key would let
        that reader observe a half-updated dict (e.g. a new ``status`` with
        a stale ``consecutive_failures``). Instead we build a fresh dict and
        swap it in with a single assignment — atomic under the GIL — so the
        reader always sees a self-consistent snapshot. Only this worker
        thread ever writes its own record, so no lock is required.
        """
        new = dict(self.health)
        new.update(changes)
        self.health = new
        self._health_dict[self._device_id] = new

    def _interruptible_sleep(self, duration: float) -> bool:
        """Sleep up to ``duration`` seconds, staying responsive to shutdown.

        Returns ``True`` if shutdown was requested during the wait (caller
        should stop), ``False`` if the full duration elapsed. The wait is
        sliced into <=100 ms chunks so a long reconnect backoff never delays
        a stop by more than a tick, even on platforms where ``Event.wait``
        wake-ups are coarse.
        """
        deadline = time.monotonic() + max(0.0, duration)
        while True:
            if self.shutdown_event.is_set():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self.shutdown_event.wait(min(0.1, remaining)):
                return True

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

    # --------------------------------------------------------------- alarms
    def _raise_offline_alarm(self) -> None:
        """Persist a connectivity alarm on the healthy->down transition.

        Fired at most once per downtime (guarded by ``_offline_alarm_raised``),
        alongside — never replacing — the ephemeral WS ``gave_up`` event. Uses
        the phase-1 reporting helper, which is idempotent per ``dedup_key`` and
        swallows its own ORM/broadcast errors, so this is safe to call from the
        worker thread. The import is local because ``reporting`` transitively
        pulls in views/serializers and this module is imported early at app
        start — a module-level import would risk a cycle (matching how the
        read loop imports ``Reading``/``time`` locally).
        """
        if self._offline_alarm_raised:
            return
        from acquisition.services.reporting import raise_system_alarm

        code = self.device.code
        raise_system_alarm(
            category="connectivity",
            severity="critical",
            message=f"设备 {code} 连接失败，已离线",
            device_code=code,
            session=self._session,
            value={
                "consecutive_failures": self.health.get("consecutive_failures", 0),
                "last_error": self._last_error,
            },
            dedup_key=f"connectivity:{code}",
        )
        self._offline_alarm_raised = True

    def _clear_offline_alarm(self) -> None:
        """Resolve the persisted connectivity alarm on recovery + re-arm.

        No-op unless an offline alarm is outstanding, so a normal connect never
        touches the ORM. Resetting ``_offline_alarm_raised`` re-arms the
        one-shot so a device that goes down again later fires a fresh alarm.
        """
        if not self._offline_alarm_raised:
            return
        from acquisition.services.reporting import clear_system_alarm

        clear_system_alarm(f"connectivity:{self.device.code}")
        self._offline_alarm_raised = False

    # ------------------------------------------------------ data-loss alarm
    #: 丢弃停止多久后自动清除数据丢弃告警(秒)。
    DROP_ALARM_CLEAR_WINDOW_S = 60.0

    def _track_dropped_messages(self) -> None:
        """把队列溢出丢弃数发布到健康记录,并管理数据丢弃告警的生命周期。

        推模式协议(MQTT/scada)在推送速率超过「队列容量×采集频率」时静默丢新
        消息 —— 压测实证 2240 msg/s 下丢了一半、界面毫无异样。这里让丢弃变成
        看得见的三件事:健康指标 dropped_messages(进会话 metadata 和 InfluxDB
        健康点)、丢弃增长时的 data_loss 系统告警(dedup 幂等)、丢弃停止
        ``DROP_ALARM_CLEAR_WINDOW_S`` 后自动清除并重新武装。

        拉模式协议没有 dropped_messages 属性,total 恒 0,全程零开销短路。
        """
        total = self._dropped_base + getattr(self.protocol, "dropped_messages", 0)
        if total == 0 and not self._drop_alarm_raised:
            return

        now = time.time()
        if total > self._last_dropped_seen:
            delta = total - self._last_dropped_seen
            self._last_dropped_seen = total
            self._last_drop_increase_at = now
            self._update_health(dropped_messages=total)
            if not self._drop_alarm_raised:
                from acquisition.services.reporting import raise_system_alarm

                code = self.device.code
                raise_system_alarm(
                    category="data_loss",
                    severity="warning",
                    message=(
                        f"设备 {code} 消息推送速率超过处理能力,接收队列溢出丢弃数据"
                        f"(本次新增 {delta} 条,累计 {total} 条)。"
                        "建议提高任务采集频率或调大接收队列容量(mqtt_queue_size)"
                    ),
                    device_code=code,
                    session=self._session,
                    value={"dropped_total": total, "dropped_delta": delta},
                    dedup_key=f"data_loss:{code}",
                )
                self._drop_alarm_raised = True
        elif (
            self._drop_alarm_raised
            and self._last_drop_increase_at is not None
            and (now - self._last_drop_increase_at) >= self.DROP_ALARM_CLEAR_WINDOW_S
        ):
            from acquisition.services.reporting import clear_system_alarm

            clear_system_alarm(f"data_loss:{self.device.code}")
            self._drop_alarm_raised = False

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
            # IMPORTANT: ``timeout`` goes through ``overrides`` so the
            # auto-derived value lands after device metadata and overrides any
            # user-supplied value rather than the other way around.
            cfg = build_device_config(
                self.device,
                overrides={"timeout": self._auto_timeout},
            )
            # 换协议实例前折叠旧实例的丢弃数,让 dropped_messages 跨重连累计。
            if self.protocol is not None:
                self._dropped_base += getattr(self.protocol, "dropped_messages", 0)
            self.protocol = ProtocolRegistry.create(self.device.protocol, cfg)
            self.protocol.connect()
            self._update_health(
                status="healthy",
                consecutive_failures=0,
                last_success=time.time(),
            )
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

            # Persistence: resolve the connectivity alarm on ANY successful
            # connect while an offline alarm is outstanding — this covers both
            # the ``reconnected`` (recovered after downtime) and the
            # first-``connected``-after-never-connecting recovery paths. Guarded
            # by the flag so it only touches the ORM on a real transition.
            self._clear_offline_alarm()

            # Reset per-cycle event flags now that we are back online.
            self._connecting_emitted = False
            self._reconnect_attempt = 0
            self._gave_up_emitted = False
            return True
        except Exception as exc:  # noqa: BLE001
            self._update_health(
                consecutive_failures=self.health["consecutive_failures"] + 1,
                status="disconnected",
            )
            self.protocol = None
            # Capture the connect error so the persisted connectivity alarm can
            # report a cause even when no read ever succeeded (a pure connect
            # failure never runs through ``_record_failure``).
            self._last_error = str(exc)
            logger.warning("ReadWorker[%s] connect failed: %s", self.device.code, exc)
            return False

    def run(self) -> None:
        # Initial connect; failure is fine — the loop will retry with backoff.
        self._connect()

        # try/finally guarantees the protocol connection is closed even if
        # the loop exits via an unexpected exception rather than a clean
        # shutdown — without it a crashed worker would leak its socket
        # until interpreter exit (and a daemon thread skips its cleanup
        # entirely when killed at exit).
        try:
            self._run_loop()
        finally:
            # Final flush so partial aggregates still surface in the UI log.
            self._flush_failed_events(force=True)
            # Graceful disconnect.
            if self.protocol is not None:
                try:
                    self.protocol.disconnect()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "ReadWorker[%s] disconnect error: %s", self.device.code, exc,
                    )
            logger.info("ReadWorker[%s] stopped", self.device.code)

    def _run_loop(self) -> None:
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
                    # Persistence: the worker has decided the device is down.
                    # Raise the connectivity alarm ONCE for this downtime (the
                    # helper is self-guarded), IN ADDITION to the ephemeral WS
                    # events above — so an offline device is recorded even when
                    # no browser is watching.
                    self._raise_offline_alarm()
                    if self._interruptible_sleep(self.reconnect_backoff):
                        break
                    # Reset so we re-attempt; otherwise we'd never recover.
                    self._update_health(consecutive_failures=0)
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
                self._track_dropped_messages()

            elapsed = time.time() - cycle_start
            sleep = max(0.0, self.cycle_interval - elapsed)
            if sleep > 0:
                if self._interruptible_sleep(sleep):
                    break

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

            self._update_health(
                last_success=time.time(),
                consecutive_failures=0,
                status="healthy",
            )

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
        last = self.health.get("last_success")
        now = time.time()
        next_failures = self.health["consecutive_failures"] + 1

        # Aggregate read_failed for the operator log.
        if self._failed_window_start is None:
            self._failed_window_start = now
        self._failed_count += 1
        self._last_error = str(exc)
        self._flush_failed_events()

        if last and (now - last) > self.connection_timeout:
            self._update_health(consecutive_failures=next_failures, status="timeout")
            try:
                if self.protocol:
                    self.protocol.disconnect()
            except Exception:  # noqa: BLE001
                pass
            if self.protocol is not None:
                self._dropped_base += getattr(self.protocol, "dropped_messages", 0)
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
            self._update_health(consecutive_failures=next_failures, status="error")
        logger.warning("ReadWorker[%s] read failed: %s", self.device.code, exc)


# Bound on each sink's flush()/close() call during Pipeline.stop() (below).
_SINK_SHUTDOWN_TIMEOUT_S = 5.0


def _run_bounded(fn, timeout: float, description: str) -> None:
    """Run ``fn()`` on a helper daemon thread and wait up to ``timeout`` s.

    Bug found under chaos testing (入库堵塞 / 优雅停止): ``InfluxDBSink.close()``
    calls ``storage.disconnect()``, which calls the influxdb-client's
    ``write_api.close()`` — a blocking call with NO timeout parameter that
    flushes pending *and retries in-flight* batches before returning. When
    InfluxDB is unreachable this was observed to block for 10s+ (bounded only
    by the client's own internal retry backoff, which can run into minutes),
    which previously blocked ``Pipeline.stop()`` — and therefore whatever
    caller (a Celery task, an API view stopping a session) invoked it —
    for just as long. That directly breaks the "graceful stop must not hang"
    contract.

    The helper thread is a daemon, so if ``fn`` truly never returns it will
    not block process exit — it is a leaked thread, not a leaked process,
    same trade-off already accepted for stuck ``ReadWorker`` threads.
    """
    done = threading.Event()
    box: Dict[str, Any] = {}

    def _target() -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            box["exc"] = exc
        finally:
            done.set()

    t = threading.Thread(target=_target, daemon=True, name=f"PipelineStop-{description}")
    t.start()
    if not done.wait(timeout=timeout):
        logger.warning(
            "Pipeline stop: %s did not complete within %.1fs — continuing "
            "shutdown without waiting further (leaked daemon thread).",
            description, timeout,
        )
        return
    if "exc" in box:
        logger.warning("Pipeline stop: %s failed: %s", description, box["exc"])


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
                session=self.session,
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
        # A worker still alive after join() is stuck in a blocking protocol
        # call. It is a daemon thread so it will not block process exit, but
        # surfacing it lets the operator see a leaked connection / hung
        # device instead of it failing silently.
        stuck = [w for w in self.workers if w.is_alive()]
        if stuck:
            logger.warning(
                "Pipeline stop: %d worker(s) did not terminate within %.1fs: %s",
                len(stuck), timeout, ", ".join(w.name for w in stuck),
            )
        for sink in self.sinks:
            name = sink.__class__.__name__
            _run_bounded(sink.flush, _SINK_SHUTDOWN_TIMEOUT_S, f"{name}.flush")
            _run_bounded(sink.close, _SINK_SHUTDOWN_TIMEOUT_S, f"{name}.close")
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
