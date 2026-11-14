"""Controlled provider for revision, recorded-decision and privacy controls."""

import hashlib
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(
    0,
    "/shared"
    if Path("/shared").is_dir()
    else str(Path(__file__).resolve().parents[2] / "lifecycle/scripts"),
)
from fixture import embedding  # noqa: E402

CANARY = "lifecycle-canary@example.invalid"


class Model:
    def __init__(self):
        self.lock = threading.Lock()
        self.revision = "A"
        self.decision_fail = False
        self.embedding_fail = False
        self.detector_hold = False
        self.pending = {}
        self.ack = True
        self.calls = []

    def infer(self, path, body):
        task = (
            "embedding"
            if path.endswith("embed") or path.endswith("embeddings")
            else "decision"
        )
        schema = (
            body.get("response_format", {}).get("json_schema", {}).get("schema", {})
        )
        if task == "decision" and "findings" in json.dumps(schema):
            task = "detector"
        with self.lock:
            identity = len(self.calls) + 1
            if identity > 10000:
                raise ValueError("audit capacity")
            revision, ack, fail = self.revision, self.ack, self.decision_fail
            embed_fail = self.embedding_fail
            gate = threading.Event()
            hold = task == "detector" and self.detector_hold
            if hold:
                self.pending[identity] = gate
            texts = body.get("input", [])
            texts = [texts] if isinstance(texts, str) else texts
            self.calls.append(
                {
                    "id": identity,
                    "task": task,
                    "revision": revision,
                    "inputs_sha256": [
                        hashlib.sha256(x.encode()).hexdigest() for x in texts
                    ],
                    "canary_matches": json.dumps(body).count(CANARY),
                    "input_sha256": hashlib.sha256(
                        json.dumps(body, sort_keys=True).encode()
                    ).hexdigest(),
                }
            )
        if hold:
            completed = gate.wait(60)
            with self.lock:
                self.pending.pop(identity, None)
            if not completed:
                raise TimeoutError("held detector expired")
        if task == "embedding":
            if embed_fail:
                raise TimeoutError("controlled embedding outage")
            texts = body["input"]
            texts = [texts] if isinstance(texts, str) else texts
            value = {
                "model": revision,
                "data": [
                    {
                        "index": i,
                        "object": "embedding",
                        "embedding": embedding(x, revision),
                    }
                    for i, x in enumerate(texts)
                ],
            }
            if ack:
                value["model_identity"] = ("a" if revision == "A" else "b") * 64
            return value
        if task == "decision":
            if fail:
                raise TimeoutError("controlled decision outage")
            prompt = json.loads(body["messages"][1]["content"])
            content = {
                "items": [
                    {"item": item["id"], "value": {"verdict": f"decision-{identity}"}}
                    for item in prompt["items"]
                ]
            }
        else:
            findings = {
                "findings": [
                    {
                        "category": "email",
                        "start": 0,
                        "end": len(CANARY),
                        "text": CANARY,
                        "confidence": 1.0,
                    }
                ]
            }
            content = (
                {"items": [{"item": 0, "value": findings}]}
                if "items" in schema.get("properties", {})
                else findings
            )
        return {
            "id": "controlled",
            "object": "chat.completion",
            "model": "fixture",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(content)},
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }


def serve():
    model = Model()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, body):
            data = json.dumps(body).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def authorized(self):
            import hmac

            return hmac.compare_digest(
                self.headers.get("Authorization", ""),
                "Bearer " + os.environ["PAPER_TOKEN"],
            )

        def do_GET(self):
            if self.path != "/control":
                return self.reply(404, {"error": "unknown endpoint"})
            if not self.authorized():
                return self.reply(403, {"error": "forbidden"})
            with model.lock:
                self.reply(
                    200,
                    {
                        "revision": model.revision,
                        "ack": model.ack,
                        "decision_fail": model.decision_fail,
                        "pending": list(model.pending),
                        "calls": list(model.calls),
                    },
                )

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length", "0"))
                if not 0 < n <= 65536:
                    raise ValueError("invalid size")
                body = json.loads(self.rfile.read(n))
                if self.path == "/control":
                    if not self.authorized():
                        return self.reply(403, {"error": "forbidden"})
                    with model.lock:
                        if "revision" in body:
                            if body["revision"] not in {"A", "B"}:
                                raise ValueError("invalid revision")
                            model.revision = body["revision"]
                        for name in [
                            "ack",
                            "decision_fail",
                            "embedding_fail",
                            "detector_hold",
                        ]:
                            if name in body:
                                if type(body[name]) is not bool:
                                    raise ValueError("invalid mode")
                                setattr(model, name, body[name])
                        if body.get("release_all") is True:
                            for gate in model.pending.values():
                                gate.set()
                    return self.reply(200, {"ok": True})
                if self.path not in {
                    "/embed",
                    "/ai/v1/embed",
                    "/v1/embeddings",
                    "/embeddings",
                    "/chat/completions",
                    "/v1/chat/completions",
                }:
                    return self.reply(404, {"error": "unknown endpoint"})
                self.reply(200, model.infer(self.path, body))
            except TimeoutError:
                self.reply(503, {"error": "controlled outage"})
            except (ValueError, KeyError, TypeError):
                self.reply(400, {"error": "invalid request"})

    server = ThreadingHTTPServer(("0.0.0.0", 8090), Handler)
    server.daemon_threads = True
    return server


if __name__ == "__main__":
    serve().serve_forever()
