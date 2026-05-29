#!/usr/bin/env python3
"""Chaos harness for Phase 2 P4 — MQTT broker kill + outbox backfill (XIU-103).

Verifies the P4 outbox-over-MQTT zero-loss guarantee: when the broker
goes down mid-flight and comes back, the edge's
:class:`~edge_agent.outbox.DurableOutbox` buffers the gap, aiomqtt
auto-reconnects, and QoS 1 PUBACK pruning produces a contiguous
``monotonic_seq`` stream on the broker / center side — no gap, no
duplicate.

There are two modes:

* **sim** (default) — the broker is a deterministic in-process double.
  Cheap, fast, hermetic; CI runs this. Exercises
  :class:`edge_agent.transport.MqttTransport` end-to-end against a fake
  ``aiomqtt.Client`` whose ``publish`` raises a ``MqttError`` for the
  duration of the simulated outage and resumes afterwards. The fake
  records every PUBACK'd publish in order so the seq stream can be
  asserted contiguous.

* **live** — drives a real local mosquitto broker. The script publishes
  a stream of frames with strictly increasing seq numbers, sends
  ``docker compose stop mosquitto`` (configurable command — note this
  is the compose *service key*, not the container_name), waits the
  chosen outage length, ``docker compose start mosquitto``,
  then validates the broker re-emits every published frame in order
  via a subscriber it owns. This is the M5/P4 smoke that the issue
  acceptance criterion ("kill mosquitto 5 minutes, recover in 60s, no
  gap") refers to. Skipped unless ``--live`` is passed.

Acceptance (per XIU-103 issue):
  *running, kill mosquitto for 5 minutes, edge outbox grows, after
  broker recovery the backfill seq has no gap within 60 s.*

Exit code 0 = passed, 1 = failed.

Usage:
  python scripts/chaos_mqtt_broker_kill.py                       # sim
  python scripts/chaos_mqtt_broker_kill.py --outage-seconds 300 --produce-rate 5
  python scripts/chaos_mqtt_broker_kill.py --live --outage-seconds 60
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple


# Make ``edge_agent`` importable when run straight from a checkout.
_REPO = Path(__file__).resolve().parents[1]
_EDGE_SRC = _REPO / "edge-agent" / "src"
if str(_EDGE_SRC) not in sys.path:
    sys.path.insert(0, str(_EDGE_SRC))

from edge_agent.outbox import DurableOutbox  # noqa: E402
from edge_agent.transport.mqtt_client import MqttTransport  # noqa: E402


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    return ok


# ---------------------------------------------------------------------------
# Simulation: fake aiomqtt client with a controllable "broker dead" window
# ---------------------------------------------------------------------------


class _SimMqttError(Exception):
    """Stand-in for ``aiomqtt.MqttError`` — the only exception type
    MqttTransport's reconnect path treats as transient."""


@dataclass
class _SimBroker:
    """In-process double of the broker + a recording subscriber.

    Every PUBACK'd publish on an uplink topic is appended to ``received``
    in arrival order — that's the stream the chaos test asserts against.
    Retained LWT publishes are recorded into ``lwt_events`` (all of
    them, in order) so the test can confirm the online-presence flip
    happened on reconnect — even though the eventual close()-time
    offline publish overwrites the last retained value.
    """

    received: List[Tuple[str, dict]] = field(default_factory=list)
    lwt_events: List[dict] = field(default_factory=list)
    dead: bool = False


class _FakeAiomqttClient:
    """aiomqtt.Client-shaped fake driven by a :class:`_SimBroker`.

    Every publish is forwarded into the broker UNLESS the broker is
    flagged ``dead`` — in which case the publish raises
    :class:`_SimMqttError`, which is what aiomqtt itself does when the
    underlying TCP socket dies. The session also raises on enter when
    the broker is dead, so the transport's reconnect loop kicks in.
    """

    def __init__(self, broker: _SimBroker) -> None:
        self._broker = broker
        self.entered = False

    async def __aenter__(self):
        if self._broker.dead:
            raise _SimMqttError("connect refused (broker dead)")
        self.entered = True
        return self

    async def __aexit__(self, *_a):
        self.entered = False
        return False

    async def publish(self, topic, payload=None, qos=0, retain=False, **_kw):
        if self._broker.dead or not self.entered:
            raise _SimMqttError("publish: broker dead")
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        body: dict
        try:
            body = json.loads((payload or b"").decode("utf-8"))
        except Exception:
            body = {"_raw": payload}
        if "/uplink/" in str(topic):
            self._broker.received.append((str(topic), body))
        elif str(topic).endswith("/lwt"):
            self._broker.lwt_events.append(body)


# ---------------------------------------------------------------------------
# Producer + uplink-loop driver
# ---------------------------------------------------------------------------


def _produce(outbox: DurableOutbox, n: int) -> None:
    """Append ``n`` uplink frames (one per call). Used to grow the
    outbox during the simulated outage."""
    for _ in range(n):
        outbox.append(lambda seq: {
            "type": "sample_batch", "monotonic_seq": seq, "task_id": 1,
            "samples": [],
        })


async def _drain_outbox(transport: MqttTransport, outbox: DurableOutbox) -> int:
    """Push every pending outbox row through the transport, pruning on
    PUBACK. Returns the number of frames delivered this pass.

    Mirrors the slim path :meth:`EdgeAgent._uplink_loop` takes when
    ``transport.confirms_on_publish`` is True (the MQTT mode): publish
    one row, prune one row.
    """
    delivered = 0
    while True:
        rows = outbox.pending(after=0, limit=200)
        if not rows:
            return delivered
        for seq, frame in rows:
            try:
                await transport.publish(frame)
            except _SimMqttError:
                # Broker dropped mid-publish: leave the row for the
                # next attempt (after the broker recovers). The
                # transport already cleared its dead session.
                return delivered
            outbox.ack(seq)
            delivered += 1


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


async def scenario_sim(
    *, outage_seconds: float, produce_rate: float, post_recover_window_s: float,
) -> bool:
    """Default chaos scenario — runs in-process, no docker.

    Models the issue's 5-minute outage as ``outage_seconds`` of
    simulated time. The simulation does not actually sleep for the
    outage; it scales the production rate down to a frame count
    (``produce_rate * outage_seconds``) and replays it through the
    outbox before flipping the broker back up. This stays under one
    second of wall clock so CI never spends real minutes on it.
    """
    outage_frames = max(1, int(round(produce_rate * outage_seconds)))
    print(
        f"scenario: sim (outage={outage_seconds:.0f}s, rate={produce_rate}/s, "
        f"outage_frames={outage_frames})"
    )

    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "edge_state.db")
        outbox = DurableOutbox(db)
        broker = _SimBroker()
        # Re-create a fake on every session so the transport's
        # __aenter__ check fires per attempt — mirroring what aiomqtt
        # does (one socket per session).
        transport = MqttTransport(
            broker_url="mqtt://sim:1883", edge_id="edge-sim",
            client_factory=lambda: _FakeAiomqttClient(broker),
        )
        transport._mqtt_error_type = lambda: (_SimMqttError,)

        # Drop the reconnect backoff so the simulation runs instantly.
        import edge_agent.transport.mqtt_client as mqc
        mqc._RECONNECT_BACKOFF_INITIAL = 0.0
        mqc._RECONNECT_BACKOFF_MAX = 0.0

        # 1) Healthy session: produce a small steady stream and drain.
        pre_outage = 50
        _produce(outbox, pre_outage)
        await transport.connect()
        delivered_pre = await _drain_outbox(transport, outbox)

        # 2) Broker drops. The transport's active session goes with
        #    it — that's how aiomqtt behaves under a real broker
        #    disconnect (paho's keepalive timeout / connection-refused
        #    surfaces as MqttError on every operation, and aiomqtt
        #    nils its underlying client). Flip the dead flag AND clear
        #    the transport's session ref so the next publish/connect
        #    has to call _open_session().
        broker.dead = True
        transport._client = None

        # 3) Edge keeps producing during the outage. Each attempt to
        #    drain raises _SimMqttError because the broker is dead.
        _produce(outbox, outage_frames)
        backlog_during_outage = outbox.depth()

        # 4) Broker comes back. aiomqtt's reconnect = our transport's
        #    next ``connect`` call. We drive recovery from here, and
        #    time-box it to ``post_recover_window_s`` (the issue's
        #    "60 s to recover with no gap").
        broker.dead = False
        t0 = time.monotonic()
        recovery_deadline = t0 + post_recover_window_s
        delivered_post = 0
        while True:
            try:
                await transport.connect()
                break
            except _SimMqttError:
                if time.monotonic() > recovery_deadline:
                    return _check("transport reconnected within window",
                                  False, "timeout")
                await asyncio.sleep(0)
        delivered_post = await _drain_outbox(transport, outbox)
        recovery_s = time.monotonic() - t0

        # 5) Post-recovery live stream — make sure the steady-state
        #    pipeline didn't get wedged by the outage.
        post_recover = 25
        _produce(outbox, post_recover)
        delivered_live = await _drain_outbox(transport, outbox)
        await transport.close()

        # ---- assertions ---------------------------------------------------
        total = pre_outage + outage_frames + post_recover
        received_seqs = [body["monotonic_seq"]
                         for _topic, body in broker.received]

        ok = True
        ok &= _check("outbox buffered the whole outage",
                     backlog_during_outage == outage_frames,
                     f"backlog={backlog_during_outage} expected={outage_frames}")
        ok &= _check(f"recovery within {post_recover_window_s:.0f}s",
                     recovery_s <= post_recover_window_s,
                     f"took {recovery_s*1000:.1f}ms")
        ok &= _check("every produced frame reached the broker",
                     len(received_seqs) == total,
                     f"received={len(received_seqs)} expected={total}")
        ok &= _check("seq stream is strictly increasing (no gap)",
                     received_seqs == list(range(1, total + 1)),
                     f"first_5={received_seqs[:5]}")
        # QoS 1 + idempotent center: duplicates are not a problem, but
        # in our deterministic sim we expect zero of them.
        ok &= _check("no duplicate seq", len(received_seqs) == len(set(received_seqs)))
        ok &= _check("outbox fully drained after recovery",
                     outbox.depth() == 0, f"depth={outbox.depth()}")
        online_count = sum(1 for ev in broker.lwt_events
                           if ev.get("state") == "online")
        ok &= _check("LWT online published on every (re)connect",
                     online_count >= 2,
                     f"online_count={online_count} events={broker.lwt_events}")
        ok &= _check("LWT offline published on graceful close",
                     bool(broker.lwt_events) and
                     broker.lwt_events[-1].get("state") == "offline",
                     f"last={broker.lwt_events[-1] if broker.lwt_events else None}")
        # Sanity: the pre-outage / post-recovery batches really did
        # land on the wire.
        ok &= _check("pre-outage frames delivered",
                     delivered_pre == pre_outage, f"got={delivered_pre}")
        ok &= _check("post-recovery live frames delivered",
                     delivered_live == post_recover, f"got={delivered_live}")
        return ok


# ---------------------------------------------------------------------------
# Live mode: drive an actual local broker through docker compose
# ---------------------------------------------------------------------------


async def scenario_live(
    *, outage_seconds: float, produce_rate: float, post_recover_window_s: float,
    compose_file: str, broker_service: str, broker_host: str, broker_port: int,
) -> bool:
    """Real-broker chaos — issue acceptance smoke.

    Spawns its own MqttTransport against a running local broker,
    publishes at ``produce_rate`` Hz, kills the broker via
    ``docker compose stop <service>`` for ``outage_seconds``, restarts
    it, and verifies the subscriber it owns sees a contiguous seq
    stream within ``post_recover_window_s`` of recovery.

    This intentionally does NOT exercise the center subscriber —
    that's an E2E concern (P5). The broker acts as the integration
    surface here.
    """
    try:
        import aiomqtt  # noqa: F401
    except ImportError:
        return _check("aiomqtt available", False,
                      "pip install aiomqtt to run --live")
    import aiomqtt as _aio

    print(
        f"scenario: live (outage={outage_seconds:.0f}s, rate={produce_rate}/s, "
        f"broker={broker_host}:{broker_port} service={broker_service})"
    )

    # Cap the transport's reconnect backoff so a single publish during the
    # outage doesn't burn the entire outage window inside one _open_session
    # call. Production default is 1s→30s exponential, which inside this
    # chaos harness means one stuck publish swallows the whole outage_target
    # loop and we under-produce frames (XIU-109).
    import edge_agent.transport.mqtt_client as mqc
    mqc._RECONNECT_BACKOFF_INITIAL = 0.5
    mqc._RECONNECT_BACKOFF_MAX = 1.0

    edge_id = f"edge-chaos-{os.getpid()}"
    received: List[int] = []
    stop_subscriber = asyncio.Event()

    async def _subscribe() -> None:
        # Auto-reconnect on broker death: mosquitto.conf has
        # ``persistence false`` and aiomqtt.Client defaults to a clean
        # session, so a broker restart loses our subscription queue and
        # we have to re-subscribe. Without this loop the subscriber dies
        # on the first ``docker stop`` MqttError and never sees the
        # backfill (XIU-109).
        while not stop_subscriber.is_set():
            try:
                async with _aio.Client(
                    hostname=broker_host, port=broker_port,
                    identifier=f"chaos-sub-{edge_id}",
                ) as sub:
                    await sub.subscribe(f"edge/{edge_id}/uplink/#", qos=1)
                    async for msg in sub.messages:
                        try:
                            body = json.loads(msg.payload.decode("utf-8"))
                        except Exception:
                            continue
                        seq = int(body.get("monotonic_seq") or 0)
                        if seq > 0:
                            received.append(seq)
                        if stop_subscriber.is_set():
                            return
            except _aio.MqttError:
                # Broker is down (or just bouncing). Wait briefly then
                # try again — give the broker time to come back up.
                await asyncio.sleep(0.5)

    sub_task = asyncio.create_task(_subscribe())

    transport = MqttTransport(
        broker_url=f"mqtt://{broker_host}:{broker_port}",
        edge_id=edge_id, client_id=f"chaos-pub-{edge_id}",
    )

    def _docker(*args: str) -> Tuple[int, str]:
        cmd = ["docker", "compose", "-f", compose_file, *args]
        print(f"  + {' '.join(cmd)}")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        return proc.returncode, (proc.stdout + proc.stderr).strip()

    # Pre-outage: connect + ship a small batch so we know the path
    # works before we start cutting things.
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "edge_state.db")
        outbox = DurableOutbox(db)
        await transport.connect()
        pre_outage = max(5, int(round(produce_rate * 2)))
        _produce(outbox, pre_outage)
        for seq, frame in outbox.pending(after=0):
            await transport.publish(frame)
            outbox.ack(seq)

        # Give the subscriber a moment to see them.
        await asyncio.sleep(0.5)

        # Cut the broker.
        rc, out = _docker("stop", broker_service)
        if rc != 0:
            stop_subscriber.set()
            sub_task.cancel()
            return _check("docker compose stop", False, out)

        # Produce continuously into the outbox during the outage. The
        # transport will keep raising on every attempted publish.
        outage_start = time.monotonic()
        outage_target = outage_start + outage_seconds
        frame_idx = pre_outage
        # Convert produce_rate to a per-iteration sleep.
        period = 1.0 / max(produce_rate, 0.1)
        # During outage we only PRODUCE — we don't attempt publishes
        # from this loop. ``MqttTransport._open_session`` blocks in a
        # ``while True:`` reconnect loop until the broker is back, and
        # the broker is only restarted AFTER this loop exits, so any
        # awaited publish here would deadlock the loop (XIU-109). The
        # post-recovery drain loop below covers the publish path.
        while time.monotonic() < outage_target:
            _produce(outbox, 1)
            frame_idx += 1
            await asyncio.sleep(period)
        backlog_during_outage = outbox.depth()

        # Restart the broker.
        rc, out = _docker("start", broker_service)
        if rc != 0:
            stop_subscriber.set()
            sub_task.cancel()
            return _check("docker compose start", False, out)

        # Let mosquitto bind + the subscriber re-subscribe before we
        # start the drain. Without this gap the publisher races ahead
        # of the subscriber and the first few backfill seqs vanish
        # because no one was listening yet (mosquitto persistence is
        # off — see mosquitto.conf — so a message published to no
        # subscribers is just dropped after PUBACK).
        await asyncio.sleep(2.0)

        # Drain the outbox once the broker is back — the transport's
        # next publish will reconnect. Time-box to the issue's 60 s.
        recovery_start = time.monotonic()
        recovery_deadline = recovery_start + post_recover_window_s
        while outbox.depth() > 0 and time.monotonic() < recovery_deadline:
            rows = outbox.pending(after=0, limit=50)
            for seq, frame in rows:
                try:
                    await transport.publish(frame)
                    outbox.ack(seq)
                except Exception:
                    # Broker still coming up — keep trying.
                    await asyncio.sleep(1.0)
                    break
            else:
                continue
        recovery_s = time.monotonic() - recovery_start

        # Settle so any in-flight QoS 1 PUBACKs land.
        await asyncio.sleep(2.0)
        await transport.close()
        stop_subscriber.set()
        sub_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sub_task

        # Snapshot anything that needs the on-disk outbox BEFORE the
        # TemporaryDirectory cleanup drops the sqlite file out from
        # under us (XIU-109).
        final_outbox_depth = outbox.depth()

    # ---- assertions -------------------------------------------------------
    expected = frame_idx
    seen = sorted(set(received))
    ok = True
    ok &= _check("outbox grew during outage",
                 backlog_during_outage > 0,
                 f"backlog={backlog_during_outage}")
    ok &= _check(f"recovered within {post_recover_window_s:.0f}s",
                 recovery_s <= post_recover_window_s,
                 f"took {recovery_s:.1f}s")
    ok &= _check("every produced seq reached the broker",
                 len(seen) >= expected,
                 f"seen={len(seen)} expected>={expected}")
    if seen:
        expected_range = list(range(seen[0], seen[-1] + 1))
        missing = sorted(set(expected_range) - set(seen))
        ok &= _check("seq stream has no gap",
                     seen == expected_range,
                     f"first={seen[0]} last={seen[-1]} unique={len(seen)}"
                     + (f" missing={missing[:10]}" if missing else ""))
    ok &= _check("outbox fully drained after recovery",
                 final_outbox_depth == 0, f"depth={final_outbox_depth}")
    return ok


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--outage-seconds", type=float, default=300.0,
                        help="simulated/real outage duration (default 300 = 5 min)")
    parser.add_argument("--produce-rate", type=float, default=2.0,
                        help="uplink frames per second produced into the outbox")
    parser.add_argument("--post-recover-window", type=float, default=60.0,
                        help="seq backfill must complete within this many seconds "
                             "of broker recovery (default 60, per XIU-103 issue)")
    parser.add_argument("--live", action="store_true",
                        help="drive a real local broker via docker compose")
    parser.add_argument("--compose-file", default="docker-compose.center.yml",
                        help="docker compose file (live mode)")
    parser.add_argument("--broker-service", default="mosquitto",
                        help="docker compose service key for the broker (live mode). "
                             "This is the key under `services:` in docker-compose.center.yml, "
                             "NOT the container_name (which is `center-mosquitto`).")
    parser.add_argument("--broker-host", default="127.0.0.1",
                        help="broker host the subscriber + publisher reach (live mode)")
    parser.add_argument("--broker-port", type=int, default=1883,
                        help="broker port (live mode)")
    args = parser.parse_args()

    if args.live:
        ok = asyncio.run(scenario_live(
            outage_seconds=args.outage_seconds,
            produce_rate=args.produce_rate,
            post_recover_window_s=args.post_recover_window,
            compose_file=args.compose_file,
            broker_service=args.broker_service,
            broker_host=args.broker_host,
            broker_port=args.broker_port,
        ))
    else:
        ok = asyncio.run(scenario_sim(
            outage_seconds=args.outage_seconds,
            produce_rate=args.produce_rate,
            post_recover_window_s=args.post_recover_window,
        ))

    print()
    print(f"chaos_mqtt_broker_kill: {'ALL PASS' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
