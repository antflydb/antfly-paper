"""Run the frozen-vector Antfly and embedded LanceDB write-path comparison serially."""

import argparse
import fcntl
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from importlib.metadata import version
from pathlib import Path

from dataset import sha256_file
from native_documents import publish
from pdf_archive_campaign import archive_binary_digest, port

HERE = Path(__file__).resolve().parent


def plan():
    pairs = [
        (engine, size) for size in (128, 512, 2048) for engine in ("antfly", "lance")
    ]
    return [
        (r, *pair)
        for r, order in enumerate((pairs, reversed(pairs)), 1)
        for pair in order
    ]


def input_digest(corpus, workload):
    checks = {p.stem for p in (corpus / "checkpoints").glob("*.json")}
    shards = {p.stem for p in (corpus / "documents").glob("*.jsonl")}
    if checks != shards or not checks or (corpus / "chunks.jsonl").exists():
        raise ValueError("expected a complete checkpointed corpus")
    digest = hashlib.sha256()
    for key in sorted(checks):
        checkpoint = json.loads((corpus / "checkpoints" / (key + ".json")).read_text())
        found = sha256_file(corpus / "documents" / (key + ".jsonl"))
        if found != checkpoint["sha256"]:
            raise ValueError("frozen vector shard changed")
        digest.update(key.encode() + b"\0" + bytes.fromhex(found))
    for p in [
        corpus / "manifest.json",
        corpus / "queries.json",
        workload / "documents.json",
        workload / "queries.json",
        workload / "judgments.json",
        workload / "provenance.json",
    ]:
        digest.update(bytes.fromhex(sha256_file(p)))
    return digest.hexdigest()


def run(args):
    args.out.mkdir(parents=True, exist_ok=True)
    args.state.mkdir(parents=True, exist_ok=True)
    with (args.out / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        identity = {
            "inputs_sha256": input_digest(args.corpus, args.workload),
            "antfly_sha256": archive_binary_digest(args.binary),
            "lancedb": version("lancedb"),
            "pyarrow": version("pyarrow"),
            "protocol_sha256": {
                name: sha256_file(HERE / name)
                for name in (
                    "compare.py",
                    "engines.py",
                    "native_release.py",
                    "write_batch_sweep.py",
                )
            },
            "probe": args.probe,
            "expected_rows": args.expected_rows,
            "order": plan(),
        }
        cohort = args.out / "cohort.json"
        if cohort.exists() and json.loads(cohort.read_text()) != json.loads(
            json.dumps(identity)
        ):
            raise ValueError("different cohort in output directory")
        publish(cohort, identity)
        failures = []
        for trial, engine, size in plan():
            key = f"r{trial}-{engine}-{size}"
            marker = args.out / (key + ".json")
            if marker.exists() and json.loads(marker.read_text()).get("success"):
                continue
            attempt = args.out / f"{key}-{time.time_ns()}"
            attempt.mkdir()
            status = {"start": time.time(), "attempt": attempt.name, "success": False}
            publish(marker, status)
            with tempfile.TemporaryDirectory(dir=args.state, prefix=key + "-") as tmp:
                db = Path(tmp) / "subject"
                if engine == "antfly":
                    cmd = [
                        HERE / "native_release.py",
                        "--binary",
                        args.binary,
                        "--expected-version",
                        "0.3.3",
                        "--port",
                        str(port()),
                        "--state",
                        db,
                    ]
                else:
                    cmd = [HERE / "compare.py", engine, "--endpoint", db]
                ranks = attempt / "rankings"
                cmd += [
                    "--b-batch-size",
                    str(size),
                    "--out",
                    ranks,
                    "--corpus",
                    args.corpus,
                    "--workload",
                    args.workload,
                    "--readiness-probe-ordinal",
                    str(args.probe),
                ]
                try:
                    with (attempt / "client.log").open("w") as log:
                        subprocess.run(
                            [sys.executable, "-u", *cmd],
                            stdout=log,
                            stderr=subprocess.STDOUT,
                            check=True,
                        )
                    result = json.loads((ranks / "result.json").read_text())
                    row_count = result["rows_per_generation"]
                    batches = [
                        json.loads(s)
                        for s in (ranks / "b-batches.jsonl").read_text().splitlines()
                    ]
                    if (
                        row_count != args.expected_rows
                        or not result["b_indexed_counts"]
                        or set(result["b_indexed_counts"].values()) != {row_count}
                        or [b["rows"] for b in batches]
                        != [min(size, row_count - i) for i in range(0, row_count, size)]
                    ):
                        raise ValueError("batch or searchable row coverage differs")
                    status.update(
                        success=True,
                        rows=row_count,
                        batches=len(batches),
                        receipt_sha256=sha256_file(ranks / "result.json"),
                        batches_sha256=sha256_file(ranks / "b-batches.jsonl"),
                    )
                except (
                    OSError,
                    ValueError,
                    KeyError,
                    subprocess.CalledProcessError,
                ) as exc:
                    failures.append(key)
                    status["error"] = str(exc)
                finally:
                    for name in ("server.log", "subject.json"):
                        if (db / name).is_file():
                            shutil.copyfile(db / name, attempt / name)
            status["end"] = time.time()
            publish(marker, status)
            print(key, status["success"], flush=True)
        return int(bool(failures))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("binary", "corpus", "workload", "state", "out"):
        parser.add_argument("--" + key, type=Path, required=True)
    parser.add_argument("--probe", type=int, default=284)
    parser.add_argument("--expected-rows", type=int, default=616832)
    raise SystemExit(run(parser.parse_args()))
