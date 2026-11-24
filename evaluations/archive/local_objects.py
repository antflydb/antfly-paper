"""Expose pinned OfficeQA PDFs as content-addressed HTTP objects on loopback."""

import argparse
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import dataset


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--port", type=int, default=38009)
    args = p.parse_args()
    inventory = dataset.verify_acquisition(args.data)
    refs, paths = {}, {}
    for entry in inventory["files"]:
        if entry["path"].endswith(".pdf"):
            path = args.data / entry["path"]
            sha = entry["local_sha256"]
            paths["/" + sha] = path
            refs[path.name] = {
                "url": f"http://127.0.0.1:{args.port}/{sha}",
                "etag": f'"{sha}"',
            }

    class Source(BaseHTTPRequestHandler):
        def do_GET(self):
            path = paths.get(self.path)
            if path is None:
                self.send_error(404)
                return
            etag = '"' + self.path[1:] + '"'
            if self.headers.get("If-Match", etag) != etag:
                self.send_error(412)
                return
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != self.path[1:]:
                self.send_error(409)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("ETag", etag)
            self.end_headers()
            self.wfile.write(raw)

    with ThreadingHTTPServer(("127.0.0.1", args.port), Source) as server:
        pending = args.manifest.with_suffix(".tmp")
        pending.write_text(json.dumps(refs, sort_keys=True))
        pending.replace(args.manifest)
        server.serve_forever()


if __name__ == "__main__":
    main()
