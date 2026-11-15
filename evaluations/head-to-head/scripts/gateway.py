"""Shared model boundary with observable holds; payloads are never logged."""

import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

lock = threading.Lock()
calls = []
held = False
allowed_inputs = frozenset()
release = threading.Event()
release.set()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, code, value, headers=None):
        data = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            if key.lower().startswith("x-antfly-") or key.lower() == "etag":
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def forward(self, path, body=None):
        headers = {
            "Content-Type": "application/json",
            **{
                k: v
                for k, v in self.headers.items()
                if k.lower().startswith("x-antfly-") or k.lower() == "if-match"
            },
        }
        req = urllib.request.Request(
            os.environ["PAPER_NATIVE_URL"] + path,
            headers=headers,
            data=None if body is None else json.dumps(body).encode(),
        )
        with urllib.request.urlopen(req, timeout=180) as response:
            return json.load(response), dict(response.headers)

    def do_GET(self):
        if self.path == "/health":
            return self.reply(200, {"ready": True})
        if urllib.parse.urlparse(self.path).path in {"/models", "/ai/v1/models"}:
            suffix = self.path[self.path.find("?") :] if "?" in self.path else ""
            try:
                value, headers = self.forward("/ai/v1/models" + suffix)
                return self.reply(200, value, headers)
            except OSError as exc:
                return self.reply(502, {"error": str(exc)[:1000]})
        if not hmac.compare_digest(
            self.headers.get("Authorization", ""), "Bearer " + os.environ["PAPER_TOKEN"]
        ):
            return self.reply(403, {})
        with lock:
            self.reply(200, {"calls": list(calls), "held": held})

    def do_POST(self):
        global held, allowed_inputs, release
        size = int(self.headers.get("Content-Length", 0))
        if not 0 < size <= 32 * 1024 * 1024:
            return self.reply(413, {})
        body = json.loads(self.rfile.read(size))
        if self.path == "/control":
            if not hmac.compare_digest(
                self.headers.get("Authorization", ""),
                "Bearer " + os.environ["PAPER_TOKEN"],
            ):
                return self.reply(403, {})
            allowed = body.get("allow_input_sha256", [])
            if (
                not isinstance(allowed, list)
                or len(allowed) > 8
                or any(
                    not isinstance(value, str)
                    or len(value) != 64
                    or any(c not in "0123456789abcdef" for c in value)
                    for value in allowed
                )
            ):
                return self.reply(400, {"error": "invalid input digest allowlist"})
            with lock:
                allowed_inputs = frozenset(allowed)
                if bool(body["hold"]) and not held:
                    release = threading.Event()
                held = bool(body["hold"])
                if not held:
                    release.set()
            return self.reply(200, {"held": held})
        path = self.path
        if path in {"/embed", "/extract", "/read"}:
            path = "/ai/v1" + path
        if path not in {
            "/ai/v1/embed",
            "/ai/v1/extract",
            "/ai/v1/read",
            "/ai/v1/chunk",
            "/ai/v1/models/instances",
        }:
            return self.reply(404, {})
        payload = json.dumps(body, sort_keys=True).encode()
        inputs = body.get("input", body.get("texts"))
        input_digest = (
            hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
            if inputs is not None
            else None
        )
        with lock:
            wait = (
                held and path == "/ai/v1/embed" and input_digest not in allowed_inputs
            )
            wait_event = release if wait else None
            row = {
                "task": path.rsplit("/", 1)[-1],
                "request_sha256": hashlib.sha256(payload).hexdigest(),
                "input_sha256": input_digest,
                "hold_applied": wait,
                "admitted": time.time(),
                "pending": True,
                "original_email_present": "@"
                in json.dumps(body.get("input", body.get("texts", []))),
            }
            calls.append(row)
        if wait_event is not None and not wait_event.wait(120):
            with lock:
                row.update(pending=False, completed=time.time(), status=504)
            return self.reply(504, {"error": "bounded hold expired"})
        try:
            value, headers = self.forward(path, body)
            with lock:
                row.update(pending=False, completed=time.time(), status=200)
            self.reply(200, value, headers)
        except urllib.error.HTTPError as exc:
            with lock:
                row.update(pending=False, status=exc.code)
            self.reply(exc.code, json.loads(exc.read()))
        except OSError as exc:
            with lock:
                row.update(pending=False, status=502)
            self.reply(502, {"error": str(exc)[:1000]})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8090), Handler).serve_forever()
