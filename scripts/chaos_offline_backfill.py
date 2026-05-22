#!/usr/bin/env python3
"""Chaos harness for M5 — offline degradation + reconnect backfill (XIU-72).

Drives the *real* edge-side durable outbox
(:class:`edge_agent.outbox.DurableOutbox`) through a center outage and
verifies the M5 zero-loss guarantee: after the center is cut and restored,
every uplink frame the edge buffered during the outage is delivered, in
order, with no gap and no duplicate.

It is a fast, deterministic, dependency-light *simulation* — no docker, no
Redis, no real WebSocket — so it runs in CI and on a laptop in seconds.
The "center down for 1 hour" of the M5 plan is modelled by the number of
frames produced while offline (``--outage-frames``, default 3600 ≈ 1 h at
1 Hz); the wall-clock hour is compressed away. The real docker pull-the-
center procedure lives in ``docs/distributed/m5-smoke.md``.

Scenarios (all run by default; pick with ``--scenario``):

  long     long online session → outage → reconnect → backfill → resume
  restart  short (<100-frame) session, edge-agent RESTARTS mid-outage,
           then reconnects — the M3 short-session-restart gap
           (see memory m3-smoke-short-session-restart-gap), closed by the
           persistent seq counter
  overflow outage longer than the buffer cap → oldest frames dropped,
           process survives, the center correctly sees one seq gap

Exit code 0 = all selected scenarios passed, 1 = a failure.

Usage:
  python scripts/chaos_offline_backfill.py
  python scripts/chaos_offline_backfill.py --scenario restart --outage-frames 50
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

# Make ``edge_agent`` importable when run straight from a checkout.
_REPO = Path(__file__).resolve().parents[1]
_EDGE_SRC = _REPO / "edge-agent" / "src"
if str(_EDGE_SRC) not in sys.path:
    sys.path.insert(0, str(_EDGE_SRC))

from edge_agent.outbox import DurableOutbox  # noqa: E402


# --- center-side seq classification ----------------------------------------
# Mirror of ``backend/fleet/models.py``::classify_uplink_seq. Kept inline so
# this script needs no Django bootstrap. MUST stay in sync with that file.
SEQ_ADVANCED = "advanced"
SEQ_GAP = "gap"
SEQ_DUPLICATE = "duplicate"


def classify_uplink_seq(prev: int, incoming: int) -> str:
    prev, incoming = int(prev or 0), int(incoming or 0)
    if prev == 0:
        return SEQ_ADVANCED
    if incoming <= prev:
        return SEQ_DUPLICATE
    if incoming == prev + 1:
        return SEQ_ADVANCED
    return SEQ_GAP


class SimCenter:
    """A stand-in center: classifies inbound seqs and records what lands."""

    def __init__(self) -> None:
        self.last_uplink_seq = 0
        self.received: list[int] = []   # seqs persisted, in arrival order
        self.duplicates = 0
        self.gaps = 0

    def register(self) -> int:
        """Edge (re)connects — report the high-water mark for backfill."""
        return self.last_uplink_seq

    def apply(self, frame: dict) -> int:
        """Idempotently record one uplink frame; return the post ack seq."""
        seq = int(frame["monotonic_seq"])
        verdict = classify_uplink_seq(self.last_uplink_seq, seq)
        if verdict == SEQ_DUPLICATE:
            self.duplicates += 1
            return self.last_uplink_seq
        if verdict == SEQ_GAP:
            self.gaps += 1
        self.received.append(seq)
        self.last_uplink_seq = seq
        return self.last_uplink_seq


def _produce(outbox: DurableOutbox, n: int) -> None:
    """Append ``n`` uplink frames to the edge's durable outbox."""
    for _ in range(n):
        outbox.append(lambda seq: {
            "type": "sample_batch", "monotonic_seq": seq, "task_id": 1,
        })


def _drain(outbox: DurableOutbox, center: SimCenter, *, batch: int = 200) -> int:
    """Replay the outbox to the center in batches; prune on ack. Returns
    the number of frames delivered this pass."""
    delivered = 0
    sent_high = center.register()
    outbox.sync_to_center(sent_high)
    while True:
        rows = outbox.pending(after=sent_high, limit=batch)
        if not rows:
            break
        for seq, frame in rows:
            ack = center.apply(frame)
            sent_high = seq
            outbox.ack(ack)
            delivered += 1
    return delivered


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def scenario_long(outage_frames: int) -> bool:
    """Long online session → outage → reconnect → backfill → resume."""
    print(f"scenario: long  (outage_frames={outage_frames})")
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "edge_state.db")
        outbox = DurableOutbox(db)
        center = SimCenter()

        online_1 = 120
        _produce(outbox, online_1)
        _drain(outbox, center)

        # --- center cut: edge keeps producing into the durable outbox ---
        _produce(outbox, outage_frames)
        backlog = outbox.depth()

        # --- center restored: reconnect + backfill ----------------------
        _drain(outbox, center)

        # --- normal real-time uplink resumes ----------------------------
        online_2 = 60
        _produce(outbox, online_2)
        _drain(outbox, center)

        total = online_1 + outage_frames + online_2
        ok = True
        ok &= _check("buffered the whole outage", backlog == outage_frames,
                     f"backlog={backlog}")
        ok &= _check("center received every event (zero loss)",
                     len(center.received) == total,
                     f"{len(center.received)}/{total}")
        ok &= _check("seq stream is contiguous 1..N",
                     center.received == list(range(1, total + 1)))
        ok &= _check("no gap, no duplicate",
                     center.gaps == 0 and center.duplicates == 0,
                     f"gaps={center.gaps} dups={center.duplicates}")
        ok &= _check("outbox fully drained", outbox.depth() == 0)
        return ok


def scenario_restart(outage_frames: int) -> bool:
    """Short (<100-frame) session, edge-agent restarts mid-outage."""
    frames = min(outage_frames, 40)
    print(f"scenario: restart  (short session, outage_frames={frames})")
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "edge_state.db")
        center = SimCenter()

        # Short online session — well under 100 frames.
        outbox = DurableOutbox(db)
        online_1 = 30
        _produce(outbox, online_1)
        _drain(outbox, center)
        seq_before = outbox.seq_high()

        # Center cut: produce a few frames, then the edge-agent CRASHES.
        _produce(outbox, frames)
        del outbox  # process exit — in-memory state gone, SQLite persists

        # Edge-agent restarts: fresh DurableOutbox on the same db file.
        outbox = DurableOutbox(db)
        ok = True
        ok &= _check("seq counter survived the restart",
                     outbox.seq_high() == seq_before + frames,
                     f"high={outbox.seq_high()}")
        ok &= _check("unacked frames survived the restart",
                     outbox.depth() == frames, f"depth={outbox.depth()}")

        # Reconnect after the restart + backfill.
        _drain(outbox, center)
        _produce(outbox, 20)
        _drain(outbox, center)

        total = online_1 + frames + 20
        ok &= _check("center received every event (zero loss)",
                     len(center.received) == total,
                     f"{len(center.received)}/{total}")
        ok &= _check("seq stream is contiguous across the restart",
                     center.received == list(range(1, total + 1)))
        ok &= _check("no gap, no duplicate (M3 short-restart gap closed)",
                     center.gaps == 0 and center.duplicates == 0,
                     f"gaps={center.gaps} dups={center.duplicates}")
        return ok


def scenario_overflow(outage_frames: int) -> bool:
    """Outage longer than the buffer cap → drop-oldest, process survives."""
    cap = 100
    frames = max(outage_frames, cap * 3)
    print(f"scenario: overflow  (cap={cap}, outage_frames={frames})")
    with tempfile.TemporaryDirectory() as tmp:
        db = str(Path(tmp) / "edge_state.db")
        outbox = DurableOutbox(db, max_rows=cap)
        center = SimCenter()

        _produce(outbox, 20)
        _drain(outbox, center)

        # Long outage that overruns the bounded buffer.
        _produce(outbox, frames)

        ok = True
        ok &= _check("buffer stayed bounded (no OOM)", outbox.depth() == cap,
                     f"depth={outbox.depth()} cap={cap}")
        ok &= _check("oldest frames were dropped",
                     outbox.dropped_total() == frames - cap,
                     f"dropped={outbox.dropped_total()}")

        _drain(outbox, center)

        # The newest `cap` frames are delivered; the dropped span shows up
        # as exactly one detected gap — overflow IS real, signalled loss.
        ok &= _check("survivors delivered after reconnect",
                     len(center.received) == 20 + cap,
                     f"received={len(center.received)}")
        ok &= _check("dropped span surfaces as one seq gap",
                     center.gaps == 1, f"gaps={center.gaps}")
        ok &= _check("process never crashed", True)
        return ok


SCENARIOS = {
    "long": scenario_long,
    "restart": scenario_restart,
    "overflow": scenario_overflow,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--scenario", choices=[*SCENARIOS, "all"], default="all",
        help="which chaos scenario to run (default: all)",
    )
    parser.add_argument(
        "--outage-frames", type=int, default=3600,
        help="frames produced while the center is cut; 3600 ≈ 1 h at 1 Hz",
    )
    args = parser.parse_args()

    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    print(f"== M5 chaos: offline degradation + backfill ({len(names)} scenario(s)) ==")
    results = {name: SCENARIOS[name](args.outage_frames) for name in names}

    print("\n== summary ==")
    for name, ok in results.items():
        print(f"  {name:9s} {'PASS' if ok else 'FAIL'}")
    all_ok = all(results.values())
    print("\nRESULT:", "PASS — zero loss verified" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
