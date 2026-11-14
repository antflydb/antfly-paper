"""Private controlled provider: hold requests, fail, or attest another revision.

Only hashed inputs and aggregate counts leave its control endpoint. Model and
hold mode are captured at request admission; changing configuration cannot
silently rewrite the identity of an old response. No external network calls.
"""

import base64
import hashlib
import hmac
import json
import os
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fixture import embedding


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.revision, self.mode = "A", "ok"
        self.calls, self.pending = [], {}

    def snapshot(self):
        with self.lock:
            return {
                "revision": self.revision,
                "mode": self.mode,
                "calls": [dict(c) for c in self.calls],
                "pending": list(self.pending),
            }

    def embed(self, texts):
        if (
            not isinstance(texts, list)
            or not texts
            or len(texts) > 128
            or any(not isinstance(x, str) for x in texts)
        ):
            raise ValueError("expected 1..128 text inputs")
        gate = threading.Event()
        with self.lock:
            if len(self.calls) >= 10000:
                raise ValueError("bounded audit exhausted")
            revision, mode = self.revision, self.mode
            identity = len(self.calls)
            call = {
                "id": identity,
                "revision": revision,
                "inputs": [hashlib.sha256(x.encode()).hexdigest() for x in texts],
                "state": "inflight",
            }
            self.calls.append(call)
            if mode == "hold":
                self.pending[identity] = gate
        completed = mode != "hold" or gate.wait(60)
        with self.lock:
            self.pending.pop(identity, None)
            call["state"] = "complete" if completed and mode != "fail" else "failed"
        if call["state"] != "complete":
            raise TimeoutError("controlled provider failure")
        return {
            "model": revision,
            "model_identity": ("a" if revision == "A" else "b") * 64,
            "object": "list",
            "data": [
                {"object": "embedding", "index": i, "embedding": embedding(x, revision)}
                for i, x in enumerate(texts)
            ],
            "usage": {"prompt_tokens": 0, "total_tokens": 0},
        }


def serve(host="0.0.0.0", port=8090):
    state = State()
    token = "Bearer " + os.environ["PAPER_TOKEN"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, value):
            data = json.dumps(value).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def authorized(self):
            return hmac.compare_digest(self.headers.get("Authorization", ""), token)

        def do_GET(self):
            if self.path != "/control":
                return self.reply(404, {"error": "not found"})
            if not self.authorized():
                return self.reply(403, {"error": "forbidden"})
            self.reply(200, state.snapshot())

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if not 0 < n <= 65536:
                    raise ValueError("invalid body size")
                body = json.loads(self.rfile.read(n))
                if self.path in {"/control", "/release"}:
                    if not self.authorized():
                        return self.reply(403, {"error": "forbidden"})
                    with state.lock:
                        if self.path == "/control":
                            revision, mode = body["revision"], body["mode"]
                            if revision not in {"A", "B"} or mode not in {
                                "ok",
                                "hold",
                                "fail",
                            }:
                                raise ValueError("invalid control")
                            state.revision, state.mode = revision, mode
                        else:
                            ids = body["ids"]
                            if not isinstance(ids, list) or any(
                                type(i) is not int or i not in state.pending
                                for i in ids
                            ):
                                raise ValueError("expected pending IDs")
                            for i in ids:
                                state.pending[i].set()
                    return self.reply(200, {"ok": True})
                if self.path not in {
                    "/embed",
                    "/ai/v1/embed",
                    "/v1/embeddings",
                    "/embeddings",
                }:
                    return self.reply(404, {"error": "not found"})
                texts = body["input"]
                result = state.embed([texts] if isinstance(texts, str) else texts)
                if body.get("encoding_format") == "base64":
                    for item in result["data"]:
                        item["embedding"] = base64.b64encode(
                            struct.pack("<2f", *item["embedding"])
                        ).decode()
                self.reply(200, result)
            except TimeoutError:
                self.reply(503, {"error": "controlled failure"})
            except (ValueError, KeyError, TypeError):
                self.reply(400, {"error": "invalid fixture request"})

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server, state


if __name__ == "__main__":
    server, _ = serve()
    server.serve_forever()
