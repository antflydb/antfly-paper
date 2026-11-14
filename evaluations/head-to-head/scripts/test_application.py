"""Negative controls for the measurement boundary, independent of real models."""

import os
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import gateway
from common import request
from traffic import drive, summarize


class ApplicationControls(unittest.TestCase):
    def test_scoped_discovery_and_capability_lease_survive_gateway(self):
        seen = []

        class Native(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                seen.append(self.path)
                self.send_response(200)
                self.send_header("X-Antfly-Capability-Token", "test-lease")
                self.end_headers()
                self.wfile.write(b'{"ready":true}')

            def do_POST(self):
                seen.append(self.headers.get("X-Antfly-Capability-Token"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"data":[]}')

        native = ThreadingHTTPServer(("127.0.0.1", 0), Native)
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), gateway.Handler)
        for server in (native, proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with patch.dict(
                os.environ,
                {
                    "PAPER_NATIVE_URL": f"http://127.0.0.1:{native.server_port}",
                    "PAPER_TOKEN": "test-control",
                },
            ):
                base = f"http://127.0.0.1:{proxy.server_port}"
                with urllib.request.urlopen(
                    base + "/ai/v1/models?model=BAAI%2Fbge&task=embed&operation=embed"
                ) as response:
                    lease = response.headers["X-Antfly-Capability-Token"]
                request(
                    base + "/embed",
                    {"input": ["protected text"]},
                    headers={"X-Antfly-Capability-Token": lease},
                )
                self.assertEqual(
                    seen,
                    [
                        "/ai/v1/models?model=BAAI%2Fbge&task=embed&operation=embed",
                        "test-lease",
                    ],
                )
                with self.assertRaises(OSError):
                    request(base + "/control", {"hold": True})
        finally:
            for server in (proxy, native):
                server.shutdown()
                server.server_close()

    def test_traffic_preserves_unauthorized_results_and_errors(self):
        class Broken:
            n = 0

            def query(self, *args):
                self.n += 1
                if self.n == 1:
                    raise TimeoutError()
                return {
                    "available": True,
                    "hits": [{"id": "secret", "body": "private@example.invalid"}],
                }

        rows = drive(
            Broken(),
            [{"tenant": "alpha", "query": "q", "topic": "orchard"}],
            lambda _: [],
            generation="a0",
            duration=1,
            rate=2,
            ledger={"secret": {"tenant": "beta", "topic": "battery"}},
        )
        result = summarize(rows)
        self.assertEqual(result["arrivals"], 2)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["unauthorized"], 1)
        self.assertEqual(result["email_like_outputs"], 1)
        self.assertEqual(result["relevant_top1"], 0)


if __name__ == "__main__":
    unittest.main()
