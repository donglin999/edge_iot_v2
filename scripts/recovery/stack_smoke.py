#!/usr/bin/env python3
"""Read-only smoke for restored static facts through HTTP/API/WebSocket."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import SplitResult, urlsplit


class SmokeError(RuntimeError):
    pass


def validate_base_url(base_url: str) -> SplitResult:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise SmokeError("smoke target must be an isolated drill HTTP origin")
    if parsed.hostname != "web" or parsed.port not in (None, 80):
        raise SmokeError("smoke target must be the internal web service on port 80")
    return parsed


def get(base_url: str, path: str) -> tuple[int, bytes, str]:
    request = urllib.request.Request(base_url.rstrip("/") + path)
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.status, response.read(), response.headers.get("content-type", "")


def json_get(base_url: str, path: str) -> Any:
    status, body, content_type = get(base_url, path)
    if status != 200 or "json" not in content_type.lower():
        raise SmokeError(f"{path} did not return HTTP 200 JSON")
    return json.loads(body.decode("utf-8"))


def result_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get("results"), list):
        return value["results"]
    raise SmokeError("API list response has an unexpected shape")


def _read_until(sock: socket.socket, marker: bytes) -> tuple[bytes, bytes]:
    data = b""
    while marker not in data:
        block = sock.recv(4096)
        if not block:
            raise SmokeError("WebSocket closed during handshake")
        data += block
        if len(data) > 64 * 1024:
            raise SmokeError("WebSocket handshake is too large")
    head, tail = data.split(marker, 1)
    return head + marker, tail


def _read_exact(sock: socket.socket, size: int, prefix: bytes) -> tuple[bytes, bytes]:
    data = prefix
    while len(data) < size:
        block = sock.recv(size - len(data))
        if not block:
            raise SmokeError("WebSocket closed before a complete frame")
        data += block
    return data[:size], data[size:]


def websocket_first_message(base_url: str) -> Any:
    parsed = validate_base_url(base_url)
    port = parsed.port or 80
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        "GET /ws/acquisition/global/ HTTP/1.1\r\n"
        f"Host: {parsed.hostname}:{port}\r\n"
        f"Origin: http://{parsed.hostname}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode("ascii")
    with socket.create_connection((parsed.hostname, port), timeout=15) as sock:
        sock.settimeout(15)
        sock.sendall(request)
        header, buffered = _read_until(sock, b"\r\n\r\n")
        lines = header.decode("iso-8859-1").split("\r\n")
        if not lines[0].startswith("HTTP/1.1 101"):
            raise SmokeError(f"WebSocket upgrade failed: {lines[0]}")
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.lower()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        if headers.get("sec-websocket-accept") != expected:
            raise SmokeError("WebSocket accept digest mismatch")

        first, buffered = _read_exact(sock, 2, buffered)
        opcode = first[0] & 0x0F
        masked = bool(first[1] & 0x80)
        length = first[1] & 0x7F
        if masked or opcode != 1:
            raise SmokeError("expected one unmasked WebSocket text frame")
        if length == 126:
            raw, buffered = _read_exact(sock, 2, buffered)
            length = struct.unpack("!H", raw)[0]
        elif length == 127:
            raw, buffered = _read_exact(sock, 8, buffered)
            length = struct.unpack("!Q", raw)[0]
        if length > 1024 * 1024:
            raise SmokeError("WebSocket frame exceeds smoke limit")
        payload, _ = _read_exact(sock, length, buffered)
        return json.loads(payload.decode("utf-8"))


def run(base_url: str) -> dict[str, Any]:
    validate_base_url(base_url)
    status, home, content_type = get(base_url, "/")
    if status != 200 or b'id="root"' not in home or "text/html" not in content_type:
        raise SmokeError("restored frontend did not return the built SPA")

    sites = result_list(json_get(base_url, "/api/config/sites/?limit=10"))
    if not any(item.get("code") == "M0-CI-SITE" for item in sites):
        raise SmokeError("restored site fixture is absent from the API")

    sessions = result_list(json_get(base_url, "/api/acquisition/sessions/?limit=10"))
    session = next((item for item in sessions if item.get("task_code") == "M0-CI-TASK"), None)
    if session is None:
        raise SmokeError("restored acquisition session is absent from the API")
    points = json_get(base_url, f"/api/acquisition/sessions/{session['id']}/data-points/?limit=10")
    if points.get("count") != 1 or points["results"][0].get("point_code") != "M0-CI-POINT":
        raise SmokeError("restored data-point fixture is absent from the API")

    alarms = result_list(json_get(base_url, "/api/acquisition/alarms/?limit=10"))
    if not any(
        item.get("point_code") == "M0-CI-POINT"
        and item.get("category") == "system"
        and item.get("status") == "firing"
        for item in alarms
    ):
        raise SmokeError("restored firing alarm is absent from the API")

    websocket = websocket_first_message(base_url)
    if websocket.get("type") != "active_sessions" or not any(
        item.get("task_code") == "M0-CI-TASK" for item in websocket.get("data", [])
    ):
        raise SmokeError("WebSocket did not expose the restored active session")
    return {
        "operation": "restored-static-fact-smoke",
        "http": "ok",
        "api": "ok",
        "websocket_initial_state": "ok",
        "static_alarm_read": "ok",
        "static_data_read": "ok",
        "session_id": session["id"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    args = parser.parse_args()
    try:
        result = run(args.base_url)
    except (OSError, ValueError, SmokeError, urllib.error.URLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
