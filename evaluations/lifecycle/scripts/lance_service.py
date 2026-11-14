"""Trusted synchronous SDK application for lifecycle comparisons.

The private HTTP boundary is measurement instrumentation, not a required
LanceDB service. Native embedding functions perform add/merge-insert inference;
metadata updates use the supported scalar update path. No async source ack.
"""

import hmac
import json
import os
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import lancedb
from lancedb.embeddings import EmbeddingFunction, get_registry
from lancedb.pydantic import LanceModel, Vector


@get_registry().register("lifecycle-controlled")
class Controlled(EmbeddingFunction):
    endpoint: str

    def ndims(self):
        return 2

    def compute_source_embeddings(self, texts, *args, **kwargs):
        texts = list(self.sanitize_input(texts))
        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps({"input": texts}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=70) as response:
            body = json.load(response)
        return [x["embedding"] for x in body["data"]]

    def compute_query_embeddings(self, texts, *args, **kwargs):
        return self.compute_source_embeddings(texts, *args, **kwargs)


class Application:
    def __init__(self):
        self.root = Path("/lance")
        self.db = lancedb.connect(str(self.root / "db"))
        self.catalog = self.root / "generations.json"
        self.generations = (
            json.loads(self.catalog.read_text()) if self.catalog.exists() else {}
        )
        self.lock = threading.RLock()

    def generation(self, name, revision):
        if name in self.generations:
            raise ValueError("generation already exists")
        if self.generations:
            source = self.db.open_table(next(iter(self.generations)))
            rows = (
                source.to_arrow().select(["id", "body", "version", "tag"]).to_pylist()
            )
        else:
            rows = []
        fn = Controlled.create(
            endpoint=f"http://provider-{revision.lower()}:8090/v1/embeddings"
        )

        class Document(LanceModel):
            id: str
            body: str = fn.SourceField()
            version: int
            tag: str
            vector: Vector(2) = fn.VectorField()

        table = self.db.create_table(name, schema=Document)
        if rows:
            table.add(rows)
        self.generations[name] = revision
        tmp = self.catalog.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.generations))
        tmp.replace(self.catalog)
        return {"ok": True}

    def write(self, identity, row):
        for name in self.generations:
            table = self.db.open_table(name)
            table.merge_insert(
                "id"
            ).when_matched_update_all().when_not_matched_insert_all().execute(
                [
                    {
                        "id": identity,
                        "tag": row.get("tag", ""),
                        "body": row["body"],
                        "version": row["version"],
                    }
                ]
            )
        return {"ok": True}

    def metadata(self, identity, row):
        for name in self.generations:
            self.db.open_table(name).update(
                where=f"id = '{identity}'", values={"tag": row["tag"]}
            )
        return {"ok": True}

    def delete(self, identity):
        for name in self.generations:
            self.db.open_table(name).delete(f"id = '{identity}'")
        return {"ok": True}

    def query(self, name):
        vector = [1, 0] if self.generations[name] == "A" else [0, 1]
        rows = (
            self.db.open_table(name)
            .search(vector)
            .limit(100)
            .select(["id", "vector"])
            .to_list()
        )
        return {row["id"]: row["vector"] for row in rows}


def serve():
    app = Application()
    token = "Bearer " + os.environ["PAPER_TOKEN"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, value):
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get("Authorization", ""), token):
                return self.reply(403, {"error": "forbidden"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ValueError("invalid length")
                body = json.loads(self.rfile.read(length))
                if any(not str(body[k]).isalnum() for k in ("name", "id") if k in body):
                    raise ValueError("fixture IDs are alphanumeric")
                if self.path == "/query":
                    # Reads of the existing generation must not be serialized
                    # behind the application's replacement-table construction.
                    return self.reply(200, app.query(body["name"]))
                with app.lock:
                    if self.path == "/ready":
                        value = {"ready": True}
                    elif self.path == "/generation":
                        value = app.generation(body["name"], body["revision"])
                    elif self.path == "/write":
                        value = app.write(body["id"], body["row"])
                    elif self.path == "/metadata":
                        value = app.metadata(body["id"], body["row"])
                    elif self.path == "/delete":
                        value = app.delete(body["id"])
                    elif self.path == "/query":
                        value = app.query(body["name"])
                    elif self.path == "/complete":
                        value = (
                            app.complete(body["name"])
                            if hasattr(app, "complete")
                            else {"complete": body["name"] in app.generations}
                        )
                    else:
                        return self.reply(404, {"error": "not found"})
                self.reply(200, value)
            except Exception as exc:
                print(type(exc).__name__ + ": " + str(exc)[:300], flush=True)
                self.reply(500, {"error": type(exc).__name__})

    return ThreadingHTTPServer(("0.0.0.0", 8091), Handler)


if __name__ == "__main__":
    serve().serve_forever()
