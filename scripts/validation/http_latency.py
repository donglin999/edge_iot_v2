#!/usr/bin/env python3
"""Measure a read-only HTTP endpoint and emit a machine-readable latency baseline."""

from __future__ import annotations

import argparse
import http.client
import json
import math
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from typing import Iterable, Optional, Sequence

MAX_RESPONSE_BYTES = 64 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Return 3xx to the caller instead of silently timing the redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        del req, fp, code, msg, headers, newurl
        return None


def nearest_rank(values: Sequence[float], percentile: float) -> float:
    """Return the nearest-rank percentile for a non-empty sequence."""
    if not values:
        raise ValueError("values must not be empty")
    if not 0 < percentile <= 100:
        raise ValueError("percentile must be in (0, 100]")
    ordered = sorted(values)
    index = max(0, math.ceil(percentile / 100 * len(ordered)) - 1)
    return ordered[index]


def safe_url(url: str) -> str:
    """Remove credentials, query strings and fragments from a reported URL."""
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def validate_url(url: str) -> str:
    """Accept explicit HTTP(S) URLs without embedded credentials."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must use http:// or https:// and include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL credentials are not allowed; use a secret-safe test endpoint")
    # Accessing port validates malformed/non-numeric values.
    _ = parsed.port
    return url


def _request(
    opener: urllib.request.OpenerDirector, url: str, timeout: float
) -> tuple[int, float]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "*/*", "User-Agent": "edge-iot-m0-baseline/1"},
        method="GET",
    )
    started = time.perf_counter()
    try:
        with opener.open(request, timeout=timeout) as response:
            response.read(MAX_RESPONSE_BYTES)
            return int(response.status), (time.perf_counter() - started) * 1000
    except urllib.error.HTTPError as exc:
        exc.read(MAX_RESPONSE_BYTES)
        return int(exc.code), (time.perf_counter() - started) * 1000


def measure(url: str, requests: int, warmup: int, timeout: float) -> dict[str, object]:
    url = validate_url(url)
    if requests < 1:
        raise ValueError("requests must be at least 1")
    if warmup < 0:
        raise ValueError("warmup must be non-negative")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")

    # Do not inherit HTTP(S)_PROXY for loopback/isolated validation endpoints.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    for _ in range(warmup):
        try:
            _request(opener, url, timeout)
        except (OSError, http.client.HTTPException, urllib.error.URLError, TimeoutError):
            # Warm-up is excluded from the report. Measurement requests below
            # still produce a structured failure result if the endpoint is down.
            pass

    timings: list[float] = []
    statuses: Counter[int] = Counter()
    errors: list[str] = []
    for _ in range(requests):
        try:
            status, elapsed_ms = _request(opener, url, timeout)
            statuses[status] += 1
            timings.append(elapsed_ms)
        except (
            OSError,
            http.client.HTTPException,
            urllib.error.URLError,
            TimeoutError,
        ) as exc:
            errors.append(type(exc).__name__)

    successes = sum(count for status, count in statuses.items() if 200 <= status < 300)
    report: dict[str, object] = {
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "url": safe_url(url),
        "requests": requests,
        "warmup": warmup,
        "timeout_seconds": timeout,
        "successes": successes,
        "failures": requests - successes,
        "status_counts": {str(key): statuses[key] for key in sorted(statuses)},
        "transport_errors": dict(sorted(Counter(errors).items())),
    }
    if timings:
        report["latency_ms"] = {
            "min": round(min(timings), 3),
            "median": round(statistics.median(timings), 3),
            "mean": round(statistics.fmean(timings), 3),
            "p95": round(nearest_rank(timings, 95), 3),
            "max": round(max(timings), 3),
        }
    return report


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Read-only HTTP(S) endpoint to measure")
    parser.add_argument("--requests", type=int, default=50)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=5.0)
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    try:
        report = measure(args.url, args.requests, args.warmup, args.timeout)
    except ValueError as exc:
        parser_message = str(exc)
        print(f"ERROR: {parser_message}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
