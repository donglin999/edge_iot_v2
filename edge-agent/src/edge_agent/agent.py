"""edge-agent core: register + heartbeat loop with reconnect backoff.

This module owns the single long-lived WebSocket connection to the
center. It is intentionally minimal for M1 — no acquisition workers,
no command channel, no buffering. M2 will plug in `backend.acquisition`
once the control-plane is stable.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from . import __version__
from .backoff import ExponentialBackoff
from .config import EdgeConfig
from .protocol import (
    FRAME_ACK,
    FRAME_ERROR,
    PROTOCOL_VERSION,
    make_heartbeat,
    make_register,
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
    ) -> None:
        self.config = config
        self.heartbeat_interval = heartbeat_interval
        self._connect = connect_factory or _default_connect
        self._backoff = backoff or ExponentialBackoff(initial=1.0, factor=2.0, cap=30.0)
        self._stop = asyncio.Event()
        self._started_at = time.monotonic()

    # ---- lifecycle ---------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Top-level loop: connect → run session → reconnect on failure."""
        while not self._stop.is_set():
            try:
                ws = await self._connect(self.config.center_url)
            except (OSError, WebSocketException, asyncio.TimeoutError) as exc:
                delay = self._backoff.next()
                logger.warning("connect failed (%s); retry in %.0fs", exc, delay)
                await self._sleep(delay)
                continue

            try:
                await self._run_session(ws)
            except ConnectionClosed as exc:
                logger.warning("connection closed by peer: code=%s reason=%s", exc.code, exc.reason)
            except Exception:
                logger.exception("session crashed")
            finally:
                with suppress(Exception):
                    await ws.close()

            if self._stop.is_set():
                break
            delay = self._backoff.next()
            logger.info("reconnecting in %.0fs", delay)
            await self._sleep(delay)

    # ---- session -----------------------------------------------------------

    async def _run_session(self, ws) -> None:
        """One register + heartbeat session."""
        await self._register(ws)
        # Successful register means we have a real edge identity — reset
        # the backoff so the next disconnect starts from 1 s again.
        self._backoff.reset()

        heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws), name="edge-agent.heartbeat")
        reader_task = asyncio.create_task(self._read_loop(ws), name="edge-agent.reader")
        try:
            done, pending = await asyncio.wait(
                {heartbeat_task, reader_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            # Surface the first failure so the outer loop logs + backs off.
            for t in done:
                t.result()
        finally:
            for t in (heartbeat_task, reader_task):
                if not t.done():
                    t.cancel()
                    with suppress(asyncio.CancelledError):
                        await t

    async def _register(self, ws) -> None:
        frame = make_register(
            edge_id=self.config.edge_id,
            token=self.config.edge_token,
            version=__version__,
            labels=self.config.labels,
        )
        await ws.send(json.dumps(frame))
        # Wait for the center's ack (or error) before flipping to heartbeat.
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        msg = self._parse(raw)
        if msg.get("type") == FRAME_ERROR:
            raise RuntimeError(f"center rejected register: {msg.get('code')} {msg.get('message')}")
        if msg.get("type") != FRAME_ACK:
            raise RuntimeError(f"expected ack to register, got: {msg!r}")
        logger.info("registered with center as edge=%s proto=%s", self.config.edge_id, PROTOCOL_VERSION)

    async def _heartbeat_loop(self, ws) -> None:
        while not self._stop.is_set():
            uptime = time.monotonic() - self._started_at
            frame = make_heartbeat(edge_id=self.config.edge_id, uptime=uptime)
            await ws.send(json.dumps(frame))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.heartbeat_interval)
            except asyncio.TimeoutError:
                pass

    async def _read_loop(self, ws) -> None:
        # M1: we only consume server frames so the WS reader doesn't stall.
        # M2+ will dispatch ack/cmd payloads.
        async for raw in ws:
            msg = self._parse(raw)
            if msg.get("type") == FRAME_ERROR:
                raise RuntimeError(f"center sent error: {msg.get('code')} {msg.get('message')}")
            logger.debug("recv %s", msg.get("type"))

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
