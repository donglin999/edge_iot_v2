"""edge-agent CLI entry point."""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

from .agent import EdgeAgent
from .config import ConfigError, EdgeConfig


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
    agent = EdgeAgent(cfg)
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
