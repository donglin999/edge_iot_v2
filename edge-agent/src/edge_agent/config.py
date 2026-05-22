"""Configuration loaded from environment variables.

Kept tiny on purpose — M1 only needs the WS endpoint, credentials, and
an optional labels dict.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict


class ConfigError(RuntimeError):
    pass


# Minimum aggregation window — below this the WS broadcast cadence and the
# uplink emit cost stop being worth it. Mirrors WebSocketSink's own floor.
_MIN_SAMPLE_WINDOW_S = 0.05


def _parse_bool(raw: str, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class EdgeConfig:
    edge_id: str
    edge_token: str
    center_url: str
    labels: Dict[str, Any]
    log_level: str = "INFO"
    # M3 uplink: 1 Hz aggregated sample_batch frames.
    uplink_samples: bool = True
    uplink_sample_window: float = 1.0

    @classmethod
    def from_env(cls, env: Dict[str, str] | None = None) -> "EdgeConfig":
        env = env if env is not None else os.environ
        missing = [k for k in ("EDGE_ID", "EDGE_TOKEN", "CENTER_URL") if not env.get(k)]
        if missing:
            raise ConfigError(f"missing required env: {', '.join(missing)}")

        labels_raw = env.get("EDGE_LABELS", "")
        if labels_raw:
            try:
                labels = json.loads(labels_raw)
            except json.JSONDecodeError as exc:
                raise ConfigError(f"EDGE_LABELS must be valid JSON: {exc}") from exc
            if not isinstance(labels, dict):
                raise ConfigError("EDGE_LABELS must decode to a JSON object")
        else:
            labels = {}

        window_raw = env.get("EDGE_UPLINK_SAMPLE_WINDOW", "")
        if window_raw:
            try:
                window = max(_MIN_SAMPLE_WINDOW_S, float(window_raw))
            except ValueError as exc:
                raise ConfigError(
                    f"EDGE_UPLINK_SAMPLE_WINDOW must be a number: {exc}"
                ) from exc
        else:
            window = 1.0

        return cls(
            edge_id=env["EDGE_ID"],
            edge_token=env["EDGE_TOKEN"],
            center_url=env["CENTER_URL"],
            labels=labels,
            log_level=env.get("LOG_LEVEL", "INFO").upper(),
            uplink_samples=_parse_bool(env.get("EDGE_UPLINK_SAMPLES", ""), True),
            uplink_sample_window=window,
        )
