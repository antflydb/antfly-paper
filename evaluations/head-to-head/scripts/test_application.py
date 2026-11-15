"""Negative controls for the measurement boundary, independent of real models."""

import concurrent.futures
import json
import os
import threading
import time
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

    def test_migration_probe_completes_while_document_embeddings_are_held(self):
        seen = []

        class Native(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                seen.append(
                    json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                )
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"data":[{"embedding":[0.6,0.8]}]}')

        native = ThreadingHTTPServer(("127.0.0.1", 0), Native)
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), gateway.Handler)
        for server in (native, proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        headers = {"Authorization": "Bearer test-control"}
        base = f"http://127.0.0.1:{proxy.server_port}"

        def audit():
            return request(base + "/audit", headers=headers)["calls"]

        def control(hold, allowed=()):
            return request(
                base + "/control",
                {"hold": hold, "allow_input_sha256": list(allowed)},
                headers=headers,
            )

        def pending_after(count):
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                rows = audit()[count:]
                if rows and rows[-1]["pending"] and rows[-1]["hold_applied"]:
                    return rows[-1]
                time.sleep(0.01)
            self.fail("embedding did not enter the hold")

        try:
            with (
                patch.dict(
                    os.environ,
                    {
                        "PAPER_NATIVE_URL": f"http://127.0.0.1:{native.server_port}",
                        "PAPER_TOKEN": "test-control",
                    },
                ),
                concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool,
            ):
                try:
                    control(False)
                    # Learn the input from initial admission, not a fixed server string.
                    probe = {"model": "A", "input": ["arbitrary validation input"]}
                    request(base + "/embed", probe)
                    digest = audit()[-1]["input_sha256"]
                    migration_probe = {**probe, "model": "B"}
                    # Minimal reproduction: a blanket hold blocks synchronous DDL.
                    control(True)
                    begin = len(audit())
                    blocked = pool.submit(request, base + "/embed", migration_probe)
                    pending_after(begin)
                    self.assertFalse(blocked.done())
                    self.assertEqual(len(seen), 1)
                    control(False)
                    blocked.result(timeout=2)
                    # The same probe succeeds under a targeted hold for model B.
                    control(True, [digest])
                    request(base + "/ai/v1/embed", migration_probe)
                    self.assertFalse(audit()[-1]["hold_applied"])
                    self.assertEqual(len(seen), 3)
                    begin = len(audit())
                    # A mixed batch containing the probe is still document work.
                    document = {"model": "B", "input": probe["input"] + ["document"]}
                    blocked = pool.submit(request, base + "/embed", document)
                    pending_after(begin)
                    self.assertFalse(blocked.done())
                    self.assertEqual(len(seen), 3)
                    with self.assertRaises(OSError):
                        request(base + "/control", {"hold": False})
                    with self.assertRaises(OSError):
                        control(True, ["invalid digest"])
                    self.assertFalse(blocked.done())
                    previous_hold = gateway.release
                    control(False)
                    blocked.result(timeout=2)
                    self.assertEqual(seen[-1], document)
                    # A later hold has no exemptions; expired holds are terminal.
                    control(True)
                    self.assertTrue(previous_hold.is_set())
                    self.assertIsNot(previous_hold, gateway.release)
                    with patch.object(gateway.release, "wait", return_value=False):
                        with self.assertRaises(OSError):
                            request(base + "/embed", migration_probe)
                    self.assertEqual(audit()[-1]["status"], 504)
                    self.assertFalse(audit()[-1]["pending"])
                    self.assertTrue(audit()[-1]["hold_applied"])
                    self.assertNotIn("arbitrary validation input", json.dumps(audit()))
                finally:
                    control(False)
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
