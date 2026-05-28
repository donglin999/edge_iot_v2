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
    # M5 backfill: when reconnecting after an offline window the durable
    # outbox is drained in batches of ``backfill_batch`` frames, pausing
    # ``backfill_pause`` seconds between full batches so a large backfill
    # does not flood the center the instant the socket comes back.
    backfill_batch: int = 200
    backfill_pause: float = 0.2
    # M5 offline buffer ceiling — the durable outbox keeps at most this
    # many un-acked frames; past it the oldest are dropped (with a
    # throttled warning) so a multi-hour center outage can never OOM the
    # edge. Default comfortably rides out the spec'd 1 h outage (~3.6k
    # frames at 1 Hz) with headroom; the chaos test lowers it to exercise
    # the overflow path.
    uplink_buffer_max: int = 10000
    # M6 read-only history HTTP server (XIU-83). The edge exposes
    # ``GET /history/points`` so the center proxy can pull samples for the
    # ``/data`` page on demand — fleet mode has no center-side raw store.
    # ``history_url`` is the externally reachable base URL the center
    # uses to reach this server; it is mirrored into the edge's ``labels``
    # dict on register so the center can resolve it without an out-of-band
    # config file. Defaults to ``http://<EDGE_ID>:<history_port>`` which
    # works in docker-compose where service names are DNS-resolvable.
    history_enabled: bool = True
    history_host: str = "0.0.0.0"
    history_port: int = 18086
    history_max_points: int = 50000
    history_url: str = ""
    # Phase 2 P3 (XIU-102) — pick which transport carries the seq'd uplink
    # stream (lifecycle / sample_batch / alarm_event). The WS connection is
    # retained in either mode for register / heartbeat / apply_config /
    # config_applied; the migration off WS for those control-plane frames
    # is later Phase-2 / Phase-3 work. ``mqtt`` is the default — Phase 2 is
    # actively rolling MQTT out — but operators can pin back to ``ws`` to
    # mirror the M5 stack for A/B / debugging.
    transport: str = "mqtt"
    mqtt_broker: str = "mqtt://mosquitto:1883"

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

        batch_raw = env.get("EDGE_BACKFILL_BATCH", "")
        if batch_raw:
            try:
                batch = max(1, int(batch_raw))
            except ValueError as exc:
                raise ConfigError(
                    f"EDGE_BACKFILL_BATCH must be an integer: {exc}"
                ) from exc
        else:
            batch = 200

        pause_raw = env.get("EDGE_BACKFILL_PAUSE_S", "")
        if pause_raw:
            try:
                pause = max(0.0, float(pause_raw))
            except ValueError as exc:
                raise ConfigError(
                    f"EDGE_BACKFILL_PAUSE_S must be a number: {exc}"
                ) from exc
        else:
            pause = 0.2

        cap_raw = env.get("EDGE_UPLINK_BUFFER_MAX", "")
        if cap_raw:
            try:
                buffer_max = max(1, int(cap_raw))
            except ValueError as exc:
                raise ConfigError(
                    f"EDGE_UPLINK_BUFFER_MAX must be an integer: {exc}"
                ) from exc
        else:
            buffer_max = 10000

        # M6 history-proxy HTTP server.
        history_enabled = _parse_bool(env.get("EDGE_HISTORY_ENABLED", ""), True)
        history_host = env.get("EDGE_HISTORY_HOST") or "0.0.0.0"
        port_raw = env.get("EDGE_HISTORY_PORT", "")
        if port_raw:
            try:
                history_port = int(port_raw)
            except ValueError as exc:
                raise ConfigError(
                    f"EDGE_HISTORY_PORT must be an integer: {exc}"
                ) from exc
            if not (1 <= history_port <= 65535):
                raise ConfigError(
                    f"EDGE_HISTORY_PORT must be 1..65535: got {history_port}"
                )
        else:
            history_port = 18086
        max_raw = env.get("EDGE_HISTORY_MAX_POINTS", "")
        if max_raw:
            try:
                history_max_points = max(1, int(max_raw))
            except ValueError as exc:
                raise ConfigError(
                    f"EDGE_HISTORY_MAX_POINTS must be an integer: {exc}"
                ) from exc
        else:
            history_max_points = 50000
        # Operator-supplied URL the center should call. Defaults are filled
        # in below so a misconfigured edge still advertises *something* the
        # center can try.
        history_url = (env.get("EDGE_HISTORY_URL") or "").strip()
        if not history_url:
            history_url = f"http://{env['EDGE_ID']}:{history_port}"

        # Mirror history_url into labels so the center can read it off
        # ``EdgeNode.labels`` without an out-of-band lookup. Operator-supplied
        # labels win (an operator who explicitly set ``history_url`` in
        # labels wants exactly that value).
        if history_enabled and "history_url" not in labels:
            labels = {**labels, "history_url": history_url}

        # Phase 2 P3: EDGE_TRANSPORT picks the uplink transport — ``mqtt``
        # (default) publishes seq'd uplink frames over MQTT QoS 1; ``ws``
        # keeps the legacy M5 behaviour of riding the WS channel.
        transport = (env.get("EDGE_TRANSPORT") or "mqtt").strip().lower()
        if transport not in ("mqtt", "ws"):
            raise ConfigError(
                f"EDGE_TRANSPORT must be 'mqtt' or 'ws': got {transport!r}"
            )
        mqtt_broker = (env.get("EDGE_MQTT_BROKER") or "mqtt://mosquitto:1883").strip()

        return cls(
            edge_id=env["EDGE_ID"],
            edge_token=env["EDGE_TOKEN"],
            center_url=env["CENTER_URL"],
            labels=labels,
            log_level=env.get("LOG_LEVEL", "INFO").upper(),
            uplink_samples=_parse_bool(env.get("EDGE_UPLINK_SAMPLES", ""), True),
            uplink_sample_window=window,
            backfill_batch=batch,
            backfill_pause=pause,
            uplink_buffer_max=buffer_max,
            history_enabled=history_enabled,
            history_host=history_host,
            history_port=history_port,
            history_max_points=history_max_points,
            history_url=history_url,
            transport=transport,
            mqtt_broker=mqtt_broker,
        )
