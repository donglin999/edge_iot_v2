"""edge-agent core: register + heartbeat + config dispatch + task running.

This module owns the single long-lived WebSocket connection to the
center. On top of the v0.1 register/heartbeat loop, M2 adds:

* ``apply_config`` ingest → persist into local SQLite (``edge_state.db``)
  via the Django ORM → spawn / stop ``AcquisitionService`` threads
* ``config_applied`` reply on success/failure
* ``task_state`` reports per task lifecycle transition

On startup, before the first WS connect, the agent replays the most
recent cached ``apply_config`` frame from disk so a power-cycled edge
continues acquiring without needing to wait for the center to reconnect.

M5 adds offline degradation + reconnect backfill:

* uplink frames (``lifecycle`` / ``sample_batch`` / ``alarm_event``) are
  persisted in a SQLite :class:`~edge_agent.outbox.DurableOutbox` *before*
  they hit the wire — they survive an edge restart in the middle of an
  outage.
* on reconnect the center reports its ``last_uplink_seq`` in the register
  ack; the agent prunes everything already delivered and backfills the
  seq gap (batched / throttled so a long backlog does not flood the
  center).
* the center acks each accepted uplink frame with its new
  ``last_uplink_seq``; the agent prunes the outbox up to that point.
"""
from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from contextlib import suppress
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from . import __version__
from .backoff import ExponentialBackoff
from .config import EdgeConfig
from .protocol import (
    CONFIG_APPLIED_ERROR,
    CONFIG_APPLIED_OK,
    FRAME_ACK,
    FRAME_APPLY_CONFIG,
    FRAME_ERROR,
    LIFECYCLE_SESSION_ONLINE,
    PROTOCOL_VERSION,
    make_alarm_event,
    make_config_applied,
    make_heartbeat,
    make_lifecycle,
    make_register,
    make_sample_batch,
    task_state_to_lifecycle_event,
)

logger = logging.getLogger("edge_agent.agent")


HEARTBEAT_INTERVAL_S = 1.0


# Pluggable so tests can fake a transport without spinning up a real WS.
ConnectFactory = Callable[[str], Awaitable[Any]]


def _origin_for(url: str) -> str:
    """Derive an HTTP Origin header from a ws:// URL.

    Django Channels' AllowedHostsOriginValidator wraps WS routes by default
    and rejects requests without an Origin header. Browsers send one
    automatically; the websockets client does not. Echoing back the
    target host as the Origin makes the validator happy in any sane
    deployment where the center's host is also in ALLOWED_HOSTS.
    """
    parsed = urlparse(url)
    scheme = "https" if parsed.scheme == "wss" else "http"
    netloc = parsed.netloc or "localhost"
    return f"{scheme}://{netloc}"


async def _default_connect(url: str):
    # ping_interval=None: we do our own heartbeat layer; we don't want
    # websockets' built-in ping racing with it.
    return await websockets.connect(
        url,
        ping_interval=None,
        open_timeout=10,
        origin=_origin_for(url),
    )


class EdgeAgent:
    """One long-lived control-plane connection with exponential reconnect."""

    def __init__(
        self,
        config: EdgeConfig,
        *,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_S,
        connect_factory: ConnectFactory | None = None,
        backoff: ExponentialBackoff | None = None,
        state_store=None,
        runner=None,
        durable_outbox=None,
    ) -> None:
        self.config = config
        self.heartbeat_interval = heartbeat_interval
        self._connect = connect_factory or _default_connect
        self._backoff = backoff or ExponentialBackoff(initial=1.0, factor=2.0, cap=30.0)
        self._stop = asyncio.Event()
        self._started_at = time.monotonic()
        # M2: config cache + task runner. Constructed lazily by
        # ``_bootstrap_runtime`` on the first ``run()`` call so unit tests
        # that drive ``_handle_apply_config`` directly can inject their own.
        self._state_store = state_store
        self._runner = runner
        # Control-plane replies (``config_applied``) that are NOT part of the
        # seq'd uplink stream — transient, fine to lose on a restart.
        self._control_outbox: "queue.Queue[dict]" = queue.Queue()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._current_ws = None
        # M5 uplink: ``lifecycle`` / ``sample_batch`` / ``alarm_event`` frames
        # go through the durable SQLite outbox so they survive an edge
        # restart and can be backfilled by seq gap after a reconnect. The
        # monotonic seq counter is persistent (it lives in the outbox),
        # replacing M3's per-process counter.
        self._durable_outbox = durable_outbox
        # Signalled whenever a frame is appended so the uplink loop wakes
        # without busy-polling SQLite.
        self._uplink_signal = threading.Event()
        # Highest seq pushed on the *current* WS session. Reset to the
        # center's high-water mark on each reconnect so unacked frames are
        # resent; never moved by an ack (acks prune, they don't un-send).
        self._uplink_sent_high = 0
        # ``last_uplink_seq`` the center reported in its register ack — the
        # backfill resume point for this session.
        self._center_last_seq = 0
        # Highest seq that existed in the outbox at reconnect time. Frames
        # at/below it are replayed history → tagged ``backfill`` on the wire
        # so the center can stamp ``EdgeNode.last_backfill_at``. Frames above
        # it are produced live during the session and ship un-tagged.
        self._backfill_through = 0
        self._sample_hook_registered = False
        self._alarm_hook_registered = False

    # ---- lifecycle ---------------------------------------------------------

    def stop(self) -> None:
        # Only flip the stop flag here — this is called from the signal
        # handler, which runs on the event-loop thread. The task runner's
        # shutdown touches the sync Django ORM (session rows) and MUST NOT
        # run on the loop thread; ``run()`` does it in an executor below.
        self._stop.set()

    async def run(self) -> None:
        """Top-level loop: connect → run session → reconnect on failure."""
        loop = asyncio.get_running_loop()
        self._loop = loop
        # Django bootstrap, migrations, and the cached-config replay all
        # touch the synchronous Django ORM. Running sync ORM on the asyncio
        # loop thread raises ``SynchronousOnlyOperation`` (XIU-61 defect C),
        # so every ORM-touching step runs in the default thread-pool
        # executor instead.
        await loop.run_in_executor(None, self._bootstrap_runtime)
        # Replay the most recent cached apply_config so power-cycled edges
        # resume acquisition immediately, without waiting for the center.
        await loop.run_in_executor(None, self._replay_cached_config)

        try:
            while not self._stop.is_set():
                try:
                    ws = await self._connect(self.config.center_url)
                except (OSError, WebSocketException, asyncio.TimeoutError) as exc:
                    delay = self._backoff.next()
                    logger.warning("connect failed (%s); retry in %.0fs", exc, delay)
                    await self._sleep(delay)
                    continue

                self._current_ws = ws
                try:
                    await self._run_session(ws)
                except ConnectionClosed as exc:
                    logger.warning("connection closed by peer: code=%s reason=%s", exc.code, exc.reason)
                except Exception:
                    logger.exception("session crashed")
                finally:
                    self._current_ws = None
                    with suppress(Exception):
                        await ws.close()

                if self._stop.is_set():
                    break
                delay = self._backoff.next()
                logger.info("reconnecting in %.0fs", delay)
                await self._sleep(delay)
        finally:
            # Drop the uplink hook so a stopped agent stops feeding the
            # (now-dead) outbox from any lingering WebSocketSink thread.
            if self._sample_hook_registered or self._alarm_hook_registered:
                with suppress(Exception):
                    from acquisition.services import uplink as acq_uplink

                    if self._sample_hook_registered:
                        acq_uplink.clear_sample_hook()
                    if self._alarm_hook_registered:
                        acq_uplink.clear_alarm_hook()
                self._sample_hook_registered = False
                self._alarm_hook_registered = False
            # Stop task threads off the loop thread — TaskRunner.stop()
            # flips AcquisitionSession rows (sync ORM).
            if self._runner is not None:
                with suppress(Exception):
                    await loop.run_in_executor(None, self._runner.stop_all)

    # ---- session -----------------------------------------------------------

    async def _run_session(self, ws) -> None:
        """One register + heartbeat session."""
        await self._register(ws)
        # Successful register means we have a real edge identity — reset
        # the backoff so the next disconnect starts from 1 s again.
        self._backoff.reset()
        # M5: reconcile the durable outbox against the center's high-water
        # mark (reported in the register ack). Prune frames the center
        # already has; the rest is the backfill set the uplink loop drains.
        await self._sync_outbox_to_center()
        # M3: announce the session is up. The center logs this into its
        # lifecycle event timeline; ``session.offline`` is synthesised
        # center-side on disconnect (a closing socket cannot send a frame).
        self._enqueue_uplink(lambda seq: make_lifecycle(
            edge_id=self.config.edge_id,
            monotonic_seq=seq,
            event=LIFECYCLE_SESSION_ONLINE,
        ))

        heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws), name="edge-agent.heartbeat")
        reader_task = asyncio.create_task(self._read_loop(ws), name="edge-agent.reader")
        control_task = asyncio.create_task(self._control_loop(ws), name="edge-agent.control")
        uplink_task = asyncio.create_task(self._uplink_loop(ws), name="edge-agent.uplink")
        session_tasks = (heartbeat_task, reader_task, control_task, uplink_task)
        try:
            done, pending = await asyncio.wait(
                set(session_tasks),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            # Surface the first failure so the outer loop logs + backs off.
            for t in done:
                t.result()
        finally:
            for t in session_tasks:
                if not t.done():
                    t.cancel()
                    with suppress(asyncio.CancelledError):
                        await t

    async def _sync_outbox_to_center(self) -> None:
        """Reconcile the durable outbox after a (re)connect.

        Prunes frames the center already confirmed and arms the uplink
        loop to resend everything still pending as the seq-gap backfill.
        """
        if self._durable_outbox is None:
            return
        loop = asyncio.get_running_loop()
        pending = await loop.run_in_executor(
            None, self._durable_outbox.sync_to_center, self._center_last_seq
        )
        # Resend from the center's high-water mark: anything still in the
        # outbox has seq > center_last_seq and gets pushed by _uplink_loop.
        self._uplink_sent_high = self._center_last_seq
        # Everything already in the outbox now is backfill history; frames
        # appended after this point are live and ship un-tagged.
        self._backfill_through = await loop.run_in_executor(
            None, self._durable_outbox.seq_high
        )
        if pending:
            logger.info(
                "edge-agent: reconnect backfill — %d frame(s) pending "
                "from seq>%d (through seq=%d)",
                pending, self._center_last_seq, self._backfill_through,
            )
        self._uplink_signal.set()

    async def _register(self, ws) -> None:
        # v0.5: advertise our persistent uplink high-water mark so the
        # center can spot an edge whose local state was wiped (seq
        # regressed) and reconcile rather than mis-classify the stream.
        edge_seq = self._durable_outbox.seq_high() if self._durable_outbox else 0
        frame = make_register(
            edge_id=self.config.edge_id,
            token=self.config.edge_token,
            version=__version__,
            labels=self.config.labels,
            uplink_seq=edge_seq,
        )
        await ws.send(json.dumps(frame))
        # Wait for the center's ack (or error) before flipping to heartbeat.
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        msg = self._parse(raw)
        if msg.get("type") == FRAME_ERROR:
            raise RuntimeError(f"center rejected register: {msg.get('code')} {msg.get('message')}")
        if msg.get("type") != FRAME_ACK:
            raise RuntimeError(f"expected ack to register, got: {msg!r}")
        # M5: the register ack carries the center's high-water uplink seq.
        # A v0.3/v0.4 center that predates M5 omits it — treat as 0 (the
        # whole outbox is then backfilled, and the center's idempotent seq
        # dedup drops anything it already had).
        try:
            self._center_last_seq = int(msg.get("last_uplink_seq") or 0)
        except (TypeError, ValueError):
            self._center_last_seq = 0
        logger.info(
            "registered with center as edge=%s proto=%s center_seq=%d",
            self.config.edge_id, PROTOCOL_VERSION, self._center_last_seq,
        )

    async def _heartbeat_loop(self, ws) -> None:
        while not self._stop.is_set():
            uptime = time.monotonic() - self._started_at
            tasks_running = len(self._runner.running_task_ids()) if self._runner else 0
            # Report the durable-outbox backlog so /fleet can show an edge
            # that is sitting on un-shipped uplink frames (积压量).
            backlog = self._durable_outbox.depth() if self._durable_outbox else 0
            frame = make_heartbeat(
                edge_id=self.config.edge_id,
                uptime=uptime,
                tasks=tasks_running,
                buffer=backlog,
            )
            await ws.send(json.dumps(frame))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.heartbeat_interval)
            except asyncio.TimeoutError:
                pass

    async def _read_loop(self, ws) -> None:
        async for raw in ws:
            msg = self._parse(raw)
            mtype = msg.get("type")
            if mtype == FRAME_ERROR:
                raise RuntimeError(f"center sent error: {msg.get('code')} {msg.get('message')}")
            if mtype == FRAME_APPLY_CONFIG:
                await self._handle_apply_config(msg)
                continue
            if mtype == FRAME_ACK:
                await self._handle_ack(msg)
                continue
            logger.debug("recv %s", mtype)

    async def _handle_ack(self, msg: dict) -> None:
        """Prune the durable outbox up to the center's confirmed seq.

        Every center ack (register / heartbeat / per-uplink-frame) carries
        ``last_uplink_seq`` once the peer is M5-aware. Frames at or below it
        are confirmed delivered, so they are dropped from the outbox — this
        is what keeps the table small on a long-lived online session.
        """
        if self._durable_outbox is None:
            return
        raw = msg.get("last_uplink_seq")
        if raw is None:
            return
        try:
            seq = int(raw)
        except (TypeError, ValueError):
            return
        if seq <= 0:
            return
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._durable_outbox.ack, seq)
        except Exception:  # noqa: BLE001
            logger.exception("edge-agent: outbox ack prune failed (seq=%d)", seq)

    async def _control_loop(self, ws) -> None:
        """Forward control-plane replies (``config_applied``) onto the WS.

        ``TaskRunner`` / ``_apply_config_blocking`` run in OS threads; they
        enqueue control frames via :meth:`enqueue_frame`, and this coroutine
        pulls them off the queue inside the event loop's thread so we never
        touch ``ws.send`` from outside the loop. Seq'd uplink frames go
        through :meth:`_uplink_loop` / the durable outbox instead.
        """
        loop = asyncio.get_running_loop()

        def _next_frame() -> Optional[dict]:
            try:
                return self._control_outbox.get(timeout=0.5)
            except queue.Empty:
                return None

        while not self._stop.is_set():
            frame = await loop.run_in_executor(None, _next_frame)
            if frame is None:
                continue
            await ws.send(json.dumps(frame))

    async def _uplink_loop(self, ws) -> None:
        """Drain the durable outbox onto the WS — backfill + steady state.

        On a fresh session ``_uplink_sent_high`` is the center's high-water
        mark, so the first pass resends every still-pending frame (the
        reconnect backfill). Frames are pushed in seq order, in batches of
        ``backfill_batch`` with a ``backfill_pause`` between *full* batches
        so a large backlog cannot flood the center the instant the socket
        comes back. Acked frames are pruned out-of-band by :meth:`_handle_ack`.
        """
        loop = asyncio.get_running_loop()
        batch_size = self.config.backfill_batch

        def _fetch_batch() -> list:
            if self._durable_outbox is None:
                return []
            return self._durable_outbox.pending(
                after=self._uplink_sent_high, limit=batch_size
            )

        while not self._stop.is_set():
            batch = await loop.run_in_executor(None, _fetch_batch)
            if not batch:
                # Nothing to send — wait for an append signal (or 0.5 s
                # tick so a missed signal can never wedge the loop).
                await loop.run_in_executor(None, self._uplink_signal.wait, 0.5)
                self._uplink_signal.clear()
                continue
            for seq, frame in batch:
                # Tag replayed history so the center can stamp
                # ``last_backfill_at``; live frames (seq above the reconnect
                # high-water) ship exactly as built.
                if seq <= self._backfill_through:
                    frame = {**frame, "backfill": True}
                await ws.send(json.dumps(frame))
                self._uplink_sent_high = seq
            # A full batch means more is likely waiting (backfill flood) —
            # pause so the center can keep up before the next batch.
            if len(batch) >= batch_size and self.config.backfill_pause > 0:
                await self._sleep(self.config.backfill_pause)

    # ---- M2: apply_config + task_state ------------------------------------

    def _bootstrap_runtime(self) -> None:
        """Construct the Django-backed state store and task runner lazily.

        Skipped when injected via ``__init__`` (tests do this). A failure
        here is logged but NOT fatal: the agent still maintains the
        control-plane connection (register + heartbeat) so the center sees
        the edge online and an operator can investigate. ``apply_config``
        frames are then rejected gracefully (see ``_handle_apply_config``).
        """
        if self._state_store is not None and self._runner is not None:
            return

        # Django setup is heavy — only pay for it when we actually need
        # to run tasks. ensure_setup is idempotent so re-entries are fine.
        try:
            from .django_setup import ensure_setup
            from .outbox import DurableOutbox
            from .runner import TaskRunner
            from .state import EdgeStateStore

            db_path = ensure_setup(sample_window=self.config.uplink_sample_window)
            if self._state_store is None:
                self._state_store = EdgeStateStore(db_path)
            if self._durable_outbox is None:
                # Same SQLite file as the rest of the edge state — the
                # outbox tables are plain KV-style, separate from the
                # Django-managed ORM tables.
                self._durable_outbox = DurableOutbox(
                    db_path, max_rows=self.config.uplink_buffer_max
                )
                logger.info(
                    "edge-agent: durable uplink outbox ready (depth=%d high_seq=%d)",
                    self._durable_outbox.depth(), self._durable_outbox.seq_high(),
                )
            if self._runner is None:
                self._runner = TaskRunner(on_state=self._emit_task_state)
            self._register_sample_hook()
            self._register_alarm_hook()
        except Exception:  # noqa: BLE001
            logger.exception(
                "edge-agent runtime bootstrap failed — control-plane will "
                "stay up but config dispatch is disabled this session"
            )

    def _register_sample_hook(self) -> None:
        """Wire the acquisition pipeline's 1 Hz windows into the uplink.

        Honors ``EDGE_UPLINK_SAMPLES``: when disabled we simply never
        register, so the WebSocketSink's emit stays a no-op. Importable
        only after ``ensure_setup`` has put ``backend`` on ``sys.path``.
        """
        if not self.config.uplink_samples:
            logger.info("edge-agent: sample uplink disabled (EDGE_UPLINK_SAMPLES)")
            return
        try:
            from acquisition.services import uplink as acq_uplink

            acq_uplink.register_sample_hook(self._on_sample_window)
            self._sample_hook_registered = True
            logger.info(
                "edge-agent: sample uplink enabled (window=%.2fs)",
                self.config.uplink_sample_window,
            )
        except Exception:  # noqa: BLE001
            logger.exception("edge-agent: could not register sample uplink hook")

    def _register_alarm_hook(self) -> None:
        """Wire the acquisition AlarmSink's fire events into the uplink (M4).

        The local AlarmSink evaluates the threshold rules pushed via
        ``apply_config``; each fired alarm is relayed here and shipped to
        the center as a v0.4 ``alarm_event`` frame. Importable only after
        ``ensure_setup`` has put ``backend`` on ``sys.path``.
        """
        try:
            from acquisition.services import uplink as acq_uplink

            acq_uplink.register_alarm_hook(self._on_alarm_event)
            self._alarm_hook_registered = True
            logger.info("edge-agent: alarm uplink enabled")
        except Exception:  # noqa: BLE001
            logger.exception("edge-agent: could not register alarm uplink hook")

    def _replay_cached_config(self) -> None:
        """Re-import the last persisted apply_config snapshot on cold start."""
        if self._state_store is None:
            return
        frame = self._state_store.load_last_frame()
        if not frame:
            logger.info("no cached apply_config to replay")
            return

        from .state import apply_frame

        try:
            cached = apply_frame(self._state_store, frame)
        except Exception:  # noqa: BLE001
            logger.exception("replay: persisting cached config failed")
            return
        logger.info(
            "replay: cached apply_config v=%s tasks=%d devices=%d",
            cached.version, len(cached.tasks), len(cached.devices),
        )
        if self._runner is not None:
            self._runner.reconcile(cached.task_ids)

    async def _handle_apply_config(self, frame: dict) -> None:
        """Persist a fresh apply_config snapshot and reconcile runners.

        The actual work — ``apply_frame`` (Django ORM writes) and
        ``runner.reconcile`` (spawns task threads, also ORM) — is sync and
        MUST NOT run on the asyncio loop thread, so it is dispatched to the
        thread-pool executor (XIU-61 defect C).
        """
        if self._state_store is None or self._runner is None:
            logger.error("apply_config received before runtime bootstrap")
            self.enqueue_frame(
                make_config_applied(
                    edge_id=self.config.edge_id,
                    version=int(frame.get("version") or 0),
                    status=CONFIG_APPLIED_ERROR,
                    error="edge runtime not ready (Django bootstrap failed)",
                )
            )
            return

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._apply_config_blocking, frame)

    def _apply_config_blocking(self, frame: dict) -> None:
        """Sync body of apply_config handling — runs in a worker thread.

        Enqueues the ``config_applied`` reply onto the thread-safe control
        outbox; the ``_control_loop`` coroutine forwards it on the WS.
        """
        version = frame.get("version")
        from .state import apply_frame

        try:
            cached = apply_frame(self._state_store, frame)
        except Exception as exc:  # noqa: BLE001
            logger.exception("apply_config v=%s failed to persist", version)
            self.enqueue_frame(
                make_config_applied(
                    edge_id=self.config.edge_id,
                    version=int(version or 0),
                    status=CONFIG_APPLIED_ERROR,
                    error=str(exc),
                )
            )
            return

        logger.info(
            "apply_config v=%s: tasks=%d devices=%d points=%d — reconciling runners",
            cached.version, len(cached.tasks), len(cached.devices), len(cached.points),
        )
        try:
            self._runner.reconcile(cached.task_ids)
        except Exception as exc:  # noqa: BLE001
            logger.exception("apply_config v=%s: runner reconcile failed", version)
            self.enqueue_frame(
                make_config_applied(
                    edge_id=self.config.edge_id,
                    version=cached.version,
                    status=CONFIG_APPLIED_ERROR,
                    error=f"reconcile failed: {exc}",
                )
            )
            return

        self.enqueue_frame(
            make_config_applied(
                edge_id=self.config.edge_id,
                version=cached.version,
                status=CONFIG_APPLIED_OK,
            )
        )

    def _emit_task_state(self, task_id: int, task_code: str, state: str, error: Optional[str]) -> None:
        """Callback used by TaskRunner — runs on a worker thread.

        M3: emits a v0.3 ``lifecycle`` frame (``task.*`` event) carrying a
        ``monotonic_seq`` instead of the v0.2 ``task_state`` frame.
        """
        try:
            event = task_state_to_lifecycle_event(state)
        except ValueError:
            logger.exception("invalid task state from runner: %r", state)
            return
        self._enqueue_uplink(lambda seq: make_lifecycle(
            edge_id=self.config.edge_id,
            monotonic_seq=seq,
            event=event,
            task_id=task_id,
            task_code=task_code,
            error=error,
        ))

    def _on_sample_window(self, window) -> None:
        """Sample-uplink hook — runs on the WebSocketSink broadcast thread.

        ``window`` is an ``acquisition.services.uplink.SampleWindow``. We
        resolve ``task_code`` from the runner's worker table (no ORM hit)
        and ship one ``sample_batch`` frame per window.
        """
        task_id = window.task_id
        if task_id is None:
            return
        task_code = str(task_id)
        if self._runner is not None:
            resolved = self._runner.task_code_for(int(task_id))
            if resolved:
                task_code = resolved
        self._enqueue_uplink(lambda seq: make_sample_batch(
            edge_id=self.config.edge_id,
            monotonic_seq=seq,
            task_id=int(task_id),
            task_code=task_code,
            samples=list(window.samples),
            window_start=window.window_start,
            window_end=window.window_end,
        ))

    def _on_alarm_event(self, event) -> None:
        """Alarm-uplink hook — runs on the AlarmSink writer thread.

        ``event`` is an ``acquisition.services.uplink.AlarmEvent``. We ship
        one ``alarm_event`` frame per alarm transition — ``firing`` and
        (M5) ``cleared`` — carrying a ``monotonic_seq`` from the same
        persistent durable-outbox counter the lifecycle / sample_batch
        frames use (one ordered uplink stream).
        """
        self._enqueue_uplink(lambda seq: make_alarm_event(
            edge_id=self.config.edge_id,
            monotonic_seq=seq,
            rule_id=int(event.rule_id),
            point_code=str(event.point_code),
            device_code=str(event.device_code or ""),
            value=event.value,
            severity=str(event.severity or "warning"),
            status=str(event.status or "firing"),
            message=str(event.message or ""),
            fired_at=event.fired_at,
        ))

    def _enqueue_uplink(self, build_frame: Callable[[int], dict]) -> None:
        """Persist an uplink frame in the durable outbox.

        :class:`~edge_agent.outbox.DurableOutbox` allocates the next
        persistent ``monotonic_seq``, builds the frame and writes it to
        SQLite atomically — so even an edge-agent crash right after this
        call cannot lose the frame. The uplink loop picks it up from disk.

        Called from several worker threads (task runner + WebSocketSink +
        AlarmSink); the outbox serialises them internally.
        """
        if self._durable_outbox is None:
            logger.warning("uplink frame dropped — durable outbox not ready")
            return
        try:
            self._durable_outbox.append(build_frame)
        except Exception:  # noqa: BLE001
            logger.exception("uplink frame persist failed")
            return
        # Wake the uplink loop so a freshly-appended frame ships promptly.
        self._uplink_signal.set()

    def enqueue_frame(self, frame: dict) -> None:
        """Thread-safe queue of a control-plane reply to the WS sender."""
        self._control_outbox.put_nowait(frame)

    # ---- helpers -----------------------------------------------------------

    @staticmethod
    def _parse(raw) -> dict:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"non-JSON frame from center: {raw!r}") from exc
        if not isinstance(obj, dict):
            raise RuntimeError(f"top-level frame is not an object: {obj!r}")
        return obj

    async def _sleep(self, seconds: float) -> None:
        # Interruptible sleep so .stop() can cut a long backoff short.
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
