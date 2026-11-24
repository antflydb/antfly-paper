"""Exercise matched, explicitly missing, and native-text pages in a released Antfly standalone."""

import argparse
import functools
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from document_study import ready
from engines import call
from native_documents import publish
from pdf_archive_campaign import port, stop
from pdf_vector_migration import MODEL, prepare


def qualify(args):
    import pypdfium2 as pdfium

    args.out.mkdir(parents=True, exist_ok=False)
    objects = args.out / "pdf"
    prepare(objects)
    pdf = objects / "replay.pdf"
    with closing(pdfium.PdfDocument.new()) as combined:
        for source in ("v1/ledger.pdf", "v2/ledger.pdf", "v1/long.pdf"):
            with closing(pdfium.PdfDocument(objects / source)) as doc:
                combined.import_pages(doc)
        combined.save(pdf)
    sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    hashes = subprocess.run(
        [args.renderer, pdf, "3"], check=True, capture_output=True, text=True
    ).stderr.splitlines()
    images = [line.split()[2] for line in hashes if line.startswith("page ")]
    if len(set(images)) != 3:
        raise ValueError("expected three distinct fixture pages")
    catalog = args.out / "catalog.sqlite"
    with closing(sqlite3.connect(catalog)) as db:
        db.executescript(
            "CREATE TABLE meta(key PRIMARY KEY,value); INSERT INTO meta VALUES ('render_complete','1'); CREATE TABLE renders(sha,page,png); CREATE TABLE pages(sha,page,body);"
        )
        with db:
            db.executemany(
                "INSERT INTO renders VALUES (?,?,?)",
                [(sha, i, d) for i, d in enumerate(images, 1)],
            )
            db.executemany(
                "INSERT INTO pages VALUES (?,?,?)",
                [
                    (
                        sha,
                        1,
                        "Quasarxylophone identifies the calibration record supplied exclusively by the replay reader for the first scanned page.",
                    ),
                    (sha, 2, None),
                    (sha, 3, "unused publisher text"),
                ],
            )
    rp = port()
    reader = subprocess.Popen(
        [
            sys.executable,
            Path(__file__).with_name("publisher_replay.py"),
            "serve",
            "--catalog",
            catalog,
            "--port",
            str(rp),
        ]
    )
    web = ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(SimpleHTTPRequestHandler, directory=objects)
    )
    threading.Thread(target=web.serve_forever, daemon=True).start()
    try:
        replay = f"http://127.0.0.1:{rp}/v1"
        ready(reader, lambda: call(replay + "/status", "GET") == {})
        q = "quasarxylophone"
        resp = call(
            args.endpoint + "/ai/v1/embed",
            "POST",
            {
                "model": MODEL,
                "input": [q],
                "task_type": "RETRIEVAL_QUERY",
                "dimensions": 768,
            },
            os.environ["ARCHIVE_ANTFLY_AUTH"],
        )
        vec = resp["data"][0]["embedding"]
        for name, payload in {
            "manifest": {
                "model_identity": resp["model_identity"],
                "counts": {"a_rows": 6},
            },
            "documents": {
                "replay.pdf": {
                    "archive": {"collection_code": "C0", "retention_years": 5}
                }
            },
            "refs": {
                "replay.pdf": {
                    "url": f"http://127.0.0.1:{web.server_port}/replay.pdf",
                    "etag": sha,
                }
            },
            "queries": [
                {
                    "id": ph,
                    "phase": ph,
                    "tenant": "public" if ph == "original" else "archive",
                    "question": q,
                    "filter": {}
                    if ph == "original"
                    else {"field": "retention_years", "equals": 5},
                    "a": vec,
                    "b": vec,
                }
                for ph in ["original", "after-schema-change"]
            ],
            "judgments": {
                "after-schema-change": {
                    "eligible": ["replay.pdf"],
                    "pages": ["replay.pdf#page=1"],
                }
            },
        }.items():
            publish(args.out / (name + ".json"), payload)
        ranks = args.out / "rankings"
        subprocess.run(
            [
                sys.executable,
                Path(__file__).with_name("pdf_native_archive.py"),
                "--endpoint",
                args.endpoint,
                "--refs",
                args.out / "refs.json",
                "--workload",
                args.out,
                "--corpus",
                args.out,
                "--out",
                ranks,
                "--replay-url",
                replay,
                "--probe",
                "1",
                "--timeout",
                "180",
            ],
            check=True,
        )
        result = json.loads((ranks / "result.json").read_text())
        lexical = json.loads((ranks / "b-original-lexical.json").read_text())[
            "original"
        ]
        counts = result["replay_requests"]
        if (
            lexical != ["replay.pdf#page=1"]
            or counts.get("rejected")
            or not counts.get("mapped")
            or not counts.get("missing")
        ):
            raise ValueError("replay failed page identity or missing-text checks")
        publish(
            args.out / "qualification.json",
            {
                "returned": lexical,
                "replay_requests": counts,
                "a_vectors": result["a_status"]["vectors"],
                "b_vectors": result["b_status"]["vectors"],
                "extraction": result["extraction"],
                "live_reads": result["live_closed_loop"],
            },
        )
    finally:
        web.shutdown()
        web.server_close()
        stop(reader)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--renderer", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--endpoint", required=True)
    qualify(p.parse_args())
