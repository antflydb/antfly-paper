"""Serve immutable fixture objects with a strong content ETag and byte-range support."""

import argparse
import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def handler(root):
    refs = json.loads((root / "objects.json").read_text())
    objects = {}
    for ref in refs:
        data = (root / ref["path"]).read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if sha != ref["sha256"]:
            raise ValueError("source digest mismatch")
        objects["/" + ref["path"]] = (data, '"' + sha + '"')

    aliases = {
        "/current/" + ref["key"]: "/" + ref["path"]
        for ref in refs
        if ref["version"] == 1
    }
    lock = threading.Lock()

    class Object(BaseHTTPRequestHandler):
        def do_PUT(self):
            # Only a listed immutable version may replace its own logical key.
            target = self.headers.get("X-Fixture-Version", "")
            with lock:
                current = aliases.get(self.path)
                if (
                    current is None
                    or target not in objects
                    or target.rsplit("/", 1)[-1] != current.rsplit("/", 1)[-1]
                ):
                    self.send_error(404)
                    return
                if self.headers.get("If-Match") != objects[current][1]:
                    self.send_error(412)
                    return
                aliases[self.path] = target
            self.send_response(204)
            self.end_headers()

        def do_GET(self):
            with lock:
                key = aliases.get(self.path, self.path)
            if key not in objects:
                self.send_error(404)
                return
            data, etag = objects[key]
            if self.headers.get("If-Match", etag) != etag:
                self.send_error(412)
                return
            first, last = 0, len(data) - 1
            byte_range = self.headers.get("Range")
            if byte_range:
                span = re.fullmatch(r"bytes=(\d+)-(\d*)", byte_range)
                if span:
                    first = int(span[1])
                    last = min(int(span[2]), last) if span[2] else last
                if not span or first > last:
                    self.send_error(416)
                    return
            self.send_response(206 if byte_range else 200)
            self.send_header("ETag", etag)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(last - first + 1))
            if byte_range:
                self.send_header("Content-Range", f"bytes {first}-{last}/{len(data)}")
            self.end_headers()
            self.wfile.write(data[first : last + 1])

    return Object


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("fixture", type=Path)
    p.add_argument("--port", type=int, default=8099)
    args = p.parse_args()
    ThreadingHTTPServer(("127.0.0.1", args.port), handler(args.fixture)).serve_forever()
