"""Run the configuration-only document pipeline on a dedicated standalone and immutable PDF URLs."""

import argparse
import fcntl
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import quote

from engines import call

CONFIG = Path(__file__).with_name("lighton-enrichments.json")
COUNTS = (
    "unit_count",
    "chunk_count",
    "ocr_attempted_count",
    "ocr_selected_count",
    "ocr_retained_embedded_count",
    "ocr_failed_count",
)


def publish(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n")
    temp.replace(path)


def manifest(base, key):
    url = (
        f"{base}/documents/{quote(key, safe='')}/artifacts/document_units_v1?detail=raw"
    )
    try:
        return call(url, "GET")
    except RuntimeError as e:
        if ": 404 " not in str(e):
            raise
        return {}


def run(args):
    refs = json.loads(args.refs.read_text())
    config = json.loads(CONFIG.read_text())
    base = args.endpoint.rstrip("/") + "/db/v1/tables/" + args.table
    identity = {
        "table": args.table,
        "refs_sha256": hashlib.sha256(args.refs.read_bytes()).hexdigest(),
        "config_sha256": hashlib.sha256(CONFIG.read_bytes()).hexdigest(),
    }
    path = args.out / "run.json"
    if path.exists():
        if json.loads(path.read_text()) != identity:
            raise ValueError("input manifest or native configuration changed")
        call(base, "GET")
    else:
        call(base, "POST", {"num_shards": 1})
        publish(path, identity)
    for name, definition in config.items():
        call(f"{base}/artifacts/{name}/enrichment", "PUT", definition)
    receipts = args.out / "documents"
    receipts.mkdir(exist_ok=True)
    totals = dict.fromkeys(COUNTS, 0)
    failures = 0
    for name, ref in sorted(refs.items()):
        key = hashlib.sha256(name.encode()).hexdigest()
        receipt = receipts / (key + ".json")
        if receipt.exists():
            row = json.loads(receipt.read_text())
        else:
            started = time.monotonic()
            doc = {
                "url": ref["url"],
                "filename": name,
                "mime_type": "application/pdf",
                "version": ref["etag"],
            }
            call(
                base + "/batch", "POST", {"inserts": {key: doc}, "sync_level": "write"}
            )
            while True:
                m = manifest(base, key)
                if m.get("source_url") == ref["url"] and m.get("merge_status") in {
                    "converged",
                    "failed",
                }:
                    break
                if time.monotonic() - started > args.timeout:
                    raise TimeoutError(f"no terminal manifest for {key}")
                time.sleep(2)
            row = {k: m.get(k, 0) for k in COUNTS}
            row.update(
                seconds=round(time.monotonic() - started, 3),
                route=m.get("route_type"),
                converged=m.get("merge_status") == "converged",
                error=m.get("last_error_code"),
            )
            publish(receipt, row)
        for k in totals:
            totals[k] += row[k]
        failures += bool(
            row["route"] != "pdf"
            or not row["converged"]
            or row["ocr_failed_count"]
            or not row["unit_count"]
        )
    publish(
        args.out / "result.json",
        {
            **identity,
            "documents": len(refs),
            "documents_with_failures": failures,
            **totals,
            "ocr_prompt_tokens": None,
            "ocr_completion_tokens": None,
        },
    )
    return int(bool(failures))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--endpoint", required=True)
    p.add_argument("--table", required=True)
    p.add_argument("--refs", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument(
        "--timeout", default=7200, type=float, help="maximum seconds per source"
    )
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        raise SystemExit(run(args))
