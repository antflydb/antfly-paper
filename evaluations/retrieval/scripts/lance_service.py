"""Private measurement facade for Embedded LanceDB; not a product service.

Keeps SDK execution inside the same engine resource budget as other engines,
with a separate, equally capped measurement client. Permission ordinals remain
trusted workload inputs; this facade is not a native LanceDB authorization API.
"""

import hmac
import json
import os
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
from adapters import Lance

adapter = Lance(os.environ["PAPER_LANCE_DIR"])
token = "Bearer " + os.environ["PAPER_TOKEN"]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def log_message(self, *args):
        pass

    def reply(self, status, value):
        payload = json.dumps(value, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        if not hmac.compare_digest(self.headers.get("Authorization", ""), token):
            self.close_connection = True
            self.reply(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 65536:
            self.close_connection = True
            self.reply(400, {"error": "invalid body size"})
            return
        try:
            body = json.loads(self.rfile.read(length))
            if self.path == "/ready":
                value = adapter.metadata
            elif self.path == "/load":
                adapter.load(
                    np.load("/corpus/vectors.npy", mmap_mode="r"),
                    np.load("/corpus/permissions.npy", mmap_mode="r"),
                )
                value = {"loaded": True}
            elif self.path == "/build":
                adapter.build()
                value = adapter.metadata
            elif self.path == "/count":
                value = adapter.authorized_count(body["cell"])
            elif self.path in ("/query", "/plan"):
                candidate = body["candidate"]
                if candidate not in adapter.candidates():
                    raise ValueError("unknown candidate")
                method = adapter.query if self.path == "/query" else adapter.explain
                value = method(
                    np.array(body["vector"], dtype=np.float32), body["cell"], candidate
                )
            elif self.path == "/global":
                value = adapter.global_top10(np.array(body["vector"], dtype=np.float32))
            else:
                self.reply(404, {"error": "unknown endpoint"})
                return
            self.reply(200, value)
        except Exception as exc:  # noqa: BLE001 - return bounded diagnostic without data
            print(type(exc).__name__ + ": " + str(exc)[:200], flush=True)
            self.reply(500, {"error": type(exc).__name__})


try:
    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
finally:
    adapter.close()
