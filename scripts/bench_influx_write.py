#!/usr/bin/env python3
"""Throughput benchmark for the InfluxDB write path (issue XIU-3 / C2).

This is a *simulation* — it needs no running InfluxDB. It models the single
architectural change made in C2: replacing the SYNCHRONOUS write_api (every
``write()`` blocks on an HTTP round-trip, effective batch == producer batch
of 50 points) with an asynchronous BATCHING write_api (``write()`` only
enqueues; the client coalesces points into HTTP requests of up to 500 points
flushed in the background).

The network round-trip cost is the explicit input ``--latency`` (seconds per
HTTP request). Everything else follows from arithmetic, so the numbers are
reproducible and defensible:

  * SYNCHRONOUS : HTTP requests = N / 50  ; producer is blocked the whole time
  * BATCHING    : HTTP requests = N / 500 ; producer never blocks on the network

Run:
    python3 scripts/bench_influx_write.py
    python3 scripts/bench_influx_write.py --points 100000 --latency 0.012
"""
from __future__ import annotations

import argparse
import queue
import threading
import time

# Producer-side batch of the legacy InfluxDBSink (kept for comparison).
SYNC_BATCH = 50
# C2 batching write-api batch_size.
BATCH_SIZE = 500


def _http_write(latency: float) -> None:
    """Simulate one HTTP write request to InfluxDB."""
    time.sleep(latency)


def run_synchronous(points: int, latency: float) -> float:
    """Legacy path: producer blocks on every 50-point flush."""
    start = time.perf_counter()
    sent = 0
    while sent < points:
        n = min(SYNC_BATCH, points - sent)
        _http_write(latency)  # producer thread blocked here
        sent += n
    return time.perf_counter() - start


def run_batching(points: int, latency: float) -> tuple[float, float]:
    """C2 path: producer only enqueues; a background writer coalesces.

    Returns ``(producer_seconds, end_to_end_seconds)``.
    """
    q: "queue.Queue[int]" = queue.Queue()
    done = threading.Event()

    def writer() -> None:
        pending = 0
        while True:
            try:
                pending += q.get(timeout=0.05)
            except queue.Empty:
                if done.is_set() and pending == 0:
                    return
                if pending == 0:
                    continue
            while pending >= BATCH_SIZE:
                _http_write(latency)
                pending -= BATCH_SIZE
            if done.is_set() and pending > 0 and q.empty():
                _http_write(latency)  # final partial batch
                pending = 0

    t = threading.Thread(target=writer, daemon=True)
    t.start()

    start = time.perf_counter()
    sent = 0
    while sent < points:
        n = min(SYNC_BATCH, points - sent)
        q.put(n)  # enqueue only — returns immediately
        sent += n
    producer_seconds = time.perf_counter() - start

    done.set()
    t.join()
    end_to_end_seconds = time.perf_counter() - start
    return producer_seconds, end_to_end_seconds


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--points", type=int, default=50_000, help="points to write")
    ap.add_argument("--latency", type=float, default=0.008,
                    help="simulated HTTP round-trip seconds per request")
    args = ap.parse_args()

    n, lat = args.points, args.latency
    print(f"InfluxDB write throughput benchmark — XIU-3 / C2")
    print(f"  points={n:,}  http_latency={lat * 1000:.1f}ms/request\n")

    sync_s = run_synchronous(n, lat)
    sync_rate = n / sync_s
    print(f"  SYNCHRONOUS  (batch={SYNC_BATCH})")
    print(f"    http_requests = {n // SYNC_BATCH:,}")
    print(f"    wall_time     = {sync_s:.2f}s")
    print(f"    throughput    = {sync_rate:,.0f} points/s  (producer blocked entire run)\n")

    prod_s, e2e_s = run_batching(n, lat)
    batch_rate = n / e2e_s
    print(f"  BATCHING     (batch={BATCH_SIZE})")
    print(f"    http_requests       = {n // BATCH_SIZE:,}")
    print(f"    end_to_end_wall     = {e2e_s:.2f}s")
    print(f"    producer_wall       = {prod_s:.3f}s  (never blocked on network)")
    print(f"    throughput          = {batch_rate:,.0f} points/s\n")

    print(f"  >>> end-to-end speedup : {batch_rate / sync_rate:.1f}x")
    print(f"  >>> producer unblocked : {sync_s / prod_s:,.0f}x faster to hand off")


if __name__ == "__main__":
    main()
