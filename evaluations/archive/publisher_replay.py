"""Versioned OfficeQA page text and a loopback replay reader for native PDF extraction."""

import argparse
import base64
import hashlib
import json
import re
import sqlite3
import subprocess
import threading
from collections import Counter
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import dataset
from corpus import publisher_pages

MODEL = "officeqa-publisher-replay-v1"


def connect(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def build(args):
    inventory = dataset.verify_acquisition(args.data)
    acq = dataset.sha256_file(args.data / "dataset-manifest.json")
    db = sqlite3.connect(args.catalog)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS docs (sha TEXT PRIMARY KEY, pages INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS pages (sha TEXT, page INTEGER, body TEXT, PRIMARY KEY(sha,page));
        CREATE TABLE IF NOT EXISTS renders (sha TEXT, page INTEGER, png TEXT NOT NULL, PRIMARY KEY(sha,page));
        CREATE INDEX IF NOT EXISTS raster ON renders(png);
    """)
    for key, value in [("acquisition", acq), ("model", MODEL)]:
        with db:
            db.execute("INSERT OR IGNORE INTO meta VALUES (?,?)", (key, value))
            if db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone() != (
                value,
            ):
                raise ValueError("catalog identity changed")
    if args.renderer:
        fingerprint = dataset.sha256_file(args.renderer)
        with db:
            db.execute(
                "INSERT OR IGNORE INTO meta VALUES ('renderer',?)", (fingerprint,)
            )
            if db.execute("SELECT value FROM meta WHERE key='renderer'").fetchone() != (
                fingerprint,
            ):
                raise ValueError("renderer changed")
    for item in inventory["files"]:
        if not item["path"].endswith(".pdf"):
            continue
        sha, count = item["local_sha256"], item["physical_pages"]
        pdf = args.data / item["path"]
        if not db.execute("SELECT 1 FROM docs WHERE sha=?", (sha,)).fetchone():
            parsed = args.data / dataset.V2.parse_dir / (pdf.stem + ".json")
            rows = {
                r["page"]: r["body"]
                for r in publisher_pages(json.loads(parsed.read_text()))
            }
            if any(n < 1 or n > count for n in rows):
                raise ValueError("publisher page outside physical inventory")
            with db:
                db.executemany(
                    "INSERT INTO pages VALUES (?,?,?)",
                    ((sha, n, rows.get(n)) for n in range(1, count + 1)),
                )
                db.execute("INSERT INTO docs VALUES (?,?)", (sha, count))
        if (
            args.renderer
            and not db.execute("SELECT 1 FROM renders WHERE sha=?", (sha,)).fetchone()
        ):
            res = subprocess.run(
                [args.renderer, pdf, str(count)],
                stderr=subprocess.PIPE,
                check=True,
                text=True,
            )
            hits = [
                (int(n), s)
                for n, s in re.findall(r"^page (\d+) ([a-f0-9]{64})$", res.stderr, re.M)
            ]
            if [n for n, _ in hits] != list(range(1, count + 1)):
                raise ValueError("incomplete native page hashes")
            with db:
                db.executemany(
                    "INSERT INTO renders VALUES (?,?,?)", ((sha, n, s) for n, s in hits)
                )
    n, present = db.execute("SELECT count(*),count(body) FROM pages").fetchone()
    if (n, present) != (118524, 115458):
        raise ValueError(f"unexpected physical page coverage {n}, {present}")
    with db:
        db.execute("INSERT OR REPLACE INTO meta VALUES ('text_complete','1')")
        if args.renderer:
            if db.execute("SELECT count(*) FROM renders").fetchone() != (n,):
                raise ValueError("incomplete native render index")
            db.execute("INSERT OR REPLACE INTO meta VALUES ('render_complete','1')")
    print(
        json.dumps(
            {
                "pages": n,
                "mapped": present,
                "missing": n - present,
                "rendered": bool(args.renderer),
            }
        ),
        flush=True,
    )
    db.close()


def page_text(db, sha, page):
    row = db.execute(
        "SELECT body FROM pages WHERE sha=? AND page=?", (sha, page)
    ).fetchone()
    if row is None:
        raise ValueError("unknown source version or page")
    return row[0]


def image_text(db, raw):
    digest = hashlib.sha256(raw).hexdigest()
    rows = db.execute(
        "SELECT DISTINCT body FROM renders JOIN pages USING(sha,page) WHERE png=? LIMIT 2",
        (digest,),
    ).fetchall()
    if len(rows) != 1:
        raise ValueError("unrecognized render" if not rows else "ambiguous render")
    return rows[0][0]


def serve(args):
    with closing(connect(args.catalog)) as db:
        if not db.execute("SELECT 1 FROM meta WHERE key='render_complete'").fetchone():
            raise ValueError("native render index incomplete")

    counts = Counter()
    lock = threading.Lock()

    class Reader(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/v1/status":
                return self.send_error(404)
            with lock:
                data = json.dumps(dict(counts)).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.path != "/v1/chat/completions":
                return self.send_error(404)
            try:
                length = int(self.headers["Content-Length"])
                if not 0 < length <= 256 * 1024**2:
                    return self.send_error(413)
                payload = json.loads(self.rfile.read(length))
                urls = [
                    part["image_url"]["url"]
                    for msg in payload["messages"]
                    if isinstance(msg["content"], list)
                    for part in msg["content"]
                    if part["type"] == "image_url"
                ]
                if (
                    payload["model"] != MODEL
                    or len(urls) != 1
                    or not urls[0].startswith("data:image/png;base64,")
                ):
                    raise ValueError("expected one rendered PDF page")
                with closing(connect(args.catalog)) as db:
                    body = image_text(
                        db, base64.b64decode(urls[0].split(",", 1)[1], validate=True)
                    )
                with lock:
                    counts["missing" if body is None else "mapped"] += 1
                data = json.dumps(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": body or ""},
                            }
                        ]
                    }
                ).encode()
            except (ValueError, KeyError, TypeError) as exc:
                with lock:
                    counts["rejected"] += 1
                self.log_error("replay rejected: %s", exc)
                return self.send_error(422)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_request(self, code="-", size="-"):
            pass

    ThreadingHTTPServer(("127.0.0.1", args.port), Reader).serve_forever()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--data", type=Path, required=True)
    b.add_argument("--renderer", type=Path)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, required=True)
    for cmd in [b, s]:
        cmd.add_argument("--catalog", type=Path, required=True)
    args = p.parse_args()
    (build if args.command == "build" else serve)(args)
