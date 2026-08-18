from __future__ import annotations

import contextlib
import http.client
import io
import json
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import http_latency
from http_latency import NoRedirect, main, measure, nearest_rank, safe_url, validate_url


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
            return
        payload = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class HttpLatencyTests(unittest.TestCase):
    def test_nearest_rank(self) -> None:
        values = list(range(1, 101))
        self.assertEqual(nearest_rank(values, 50), 50)
        self.assertEqual(nearest_rank(values, 95), 95)
        self.assertEqual(nearest_rank([3.0], 95), 3.0)

    def test_nearest_rank_rejects_invalid_input(self) -> None:
        with self.assertRaises(ValueError):
            nearest_rank([], 95)
        with self.assertRaises(ValueError):
            nearest_rank([1.0], 0)

    def test_safe_url_redacts_credentials_query_and_fragment(self) -> None:
        self.assertEqual(
            safe_url("http://user:secret@127.0.0.1:8000/api/?token=secret#x"),
            "http://127.0.0.1:8000/api/",
        )
        self.assertEqual(
            safe_url("http://user:secret@[::1]:8086/health?token=secret"),
            "http://[::1]:8086/health",
        )

    def test_url_validation_rejects_non_http_and_credentials(self) -> None:
        self.assertEqual(
            validate_url("https://example.test/health"),
            "https://example.test/health",
        )
        with self.assertRaises(ValueError):
            validate_url("file:///etc/passwd")
        with self.assertRaises(ValueError):
            validate_url("http://user:secret@example.test/health")

    def test_redirect_handler_never_follows(self) -> None:
        handler = NoRedirect()
        self.assertIsNone(
            handler.redirect_request(None, None, 302, "Found", {}, "http://example.test/")
        )

    def test_warmup_failure_still_returns_structured_measurement(self) -> None:
        with mock.patch.object(
            http_latency,
            "_request",
            side_effect=[
                urllib.error.URLError("warmup unavailable"),
                urllib.error.URLError("measurement unavailable"),
                http.client.IncompleteRead(b"partial", 32),
                (503, 1.25),
            ],
        ):
            report = measure(
                "http://127.0.0.1/health", requests=3, warmup=1, timeout=1
            )

        self.assertEqual(report["successes"], 0)
        self.assertEqual(report["failures"], 3)
        self.assertEqual(report["status_counts"], {"503": 1})
        self.assertEqual(
            report["transport_errors"], {"IncompleteRead": 1, "URLError": 1}
        )

    def test_cli_measures_read_only_endpoint(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                exit_code = main(
                    [
                        f"http://127.0.0.1:{server.server_port}/health?token=hidden",
                        "--requests",
                        "4",
                        "--warmup",
                        "1",
                    ]
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        report = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["successes"], 4)
        self.assertEqual(report["failures"], 0)
        self.assertEqual(report["status_counts"], {"200": 4})
        self.assertNotIn("hidden", report["url"])
        self.assertIn("p95", report["latency_ms"])

    def test_redirect_is_reported_as_failure(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            report = measure(
                f"http://127.0.0.1:{server.server_port}/redirect",
                requests=1,
                warmup=0,
                timeout=2,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(report["successes"], 0)
        self.assertEqual(report["failures"], 1)
        self.assertEqual(report["status_counts"], {"302": 1})


if __name__ == "__main__":
    unittest.main()
