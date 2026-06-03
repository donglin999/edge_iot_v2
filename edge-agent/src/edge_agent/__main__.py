"""edge-agent CLI entry point."""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Optional

from .agent import EdgeAgent
from .config import ConfigError, EdgeConfig
from .transport import MqttTransport, Transport


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, agent: EdgeAgent) -> None:
    def _stop() -> None:
        logging.getLogger("edge_agent").info("signal received, stopping")
        agent.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except (NotImplementedError, RuntimeError):
            # Windows / some embedded loops can't install signal handlers;
            # fall back to default SIGINT behavior (KeyboardInterrupt).
            pass


def _build_uplink_transport(cfg: EdgeConfig) -> Optional[Transport]:
    """Pick the uplink transport per :data:`EdgeConfig.transport`.

    Returns ``None`` to mean "use the legacy per-session WsTransport built
    inside the agent" — the M5 behaviour. An ``mqtt`` config returns a
    standalone :class:`MqttTransport` whose broker session is independent
    of the WS reconnect loop (Phase 2 P3 — XIU-102).
    """
    if cfg.transport == "ws":
        return None
    if cfg.transport == "mqtt":
        return MqttTransport(
            broker_url=cfg.mqtt_broker,
            edge_id=cfg.edge_id,
            presence_interval=cfg.mqtt_presence_interval,
        )
    # ``EdgeConfig.from_env`` already rejects unknown values, but be loud
    # if a programmatic caller constructed a bad config directly.
    raise ConfigError(f"unsupported transport: {cfg.transport!r}")


async def _amain() -> int:
    try:
        cfg = EdgeConfig.from_env()
    except ConfigError as exc:
        print(f"edge-agent: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uplink = _build_uplink_transport(cfg)
    if uplink is not None:
        logging.getLogger("edge_agent").info(
            "edge-agent: uplink transport=%s broker=%s",
            cfg.transport, cfg.mqtt_broker,
        )
    agent = EdgeAgent(cfg, uplink_transport=uplink)
    _install_signal_handlers(asyncio.get_running_loop(), agent)
    await agent.run()
    return 0


def main() -> int:
    try:
        return asyncio.run(_amain())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
