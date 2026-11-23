"""Measure the same 408 hybrid OfficeQA queries before and during paced writes to B."""

import argparse
import hashlib
import json
import math
import os
import resource
import secrets
import subprocess
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from itertools import islice
from pathlib import Path

from compare import observations, wait_for_indexed_rows
from corpus import chunk_rows
from dataset import sha256_file
from engines import Antfly, call
from native_documents import publish
from pdf_archive_campaign import port, stop


def write_batches(corpus):
    batch = []
    for row in chunk_rows(corpus):
        for tenant, fields in dict(row["views"], public={}).items():
            identity = hashlib.sha256((tenant + "\0" + row["id"]).encode()).hexdigest()
            batch.append(
                dict(
                    id=identity,
                    body=row["body"],
                    tenant=tenant,
                    page=row["page"],
                    vec=row["b"],
                    **fields,
                )
            )
            if len(batch) == 128:
                yield batch
                batch = []


def pause_until(target):
    time.sleep(max(0, target - time.monotonic()))


def backfill(engine, batches, start, interval):
    timings = []
    for i, batch in enumerate(batches):
        pause_until(start + i * interval)
        sent = time.monotonic()
        engine.insert(batch)
        timings.append([sent - start, time.monotonic() - start])
    return timings


def traffic(engine, queries, eligible, start):
    def read(q, due):
        begin = time.monotonic()
        stages = {}
        row = dict(id=q["id"], scheduled=due - start, dispatch=begin - start)
        try:
            pages = observations(
                engine, q, "a", "hybrid", 100, 20, eligible[q["id"]], stages
            )
            row.update(
                channels=stages,
                digest=hashlib.sha256(json.dumps(pages).encode()).hexdigest(),
            )
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            row["error"] = (
                "ineligible"
                if "ineligible candidate" in str(exc)
                else type(exc).__name__
            )
        row["latency"] = time.monotonic() - due
        return row

    rows = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        pending = []
        for i, q in enumerate(queries):
            due = start + i / 2
            pause_until(due)
            for job in pending.copy():
                if job.done():
                    rows.append(job.result())
                    pending.remove(job)
            if len(pending) == 8:
                rows.append(
                    dict(id=q["id"], scheduled=due - start, error="concurrency_limit")
                )
                continue
            pending.append(pool.submit(read, q, due))
        rows.extend(job.result() for job in pending)
    return sorted(rows, key=lambda r: r["scheduled"])


def summarize(rows):
    def p95(values):
        return round(1000 * sorted(values)[math.ceil(0.95 * len(values)) - 1], 3)

    good = [r for r in rows if "digest" in r]
    errors = Counter(r["error"] for r in rows if "error" in r)
    return {
        "scheduled": len(rows),
        "completed": len(good),
        "missed": errors.get("concurrency_limit", 0),
        "errors": dict(errors),
        "ineligible": errors.get("ineligible", 0),
        "p95_ms": p95([r["latency"] for r in good]) if good else None,
        "lexical_p95_ms": p95([r["channels"]["lexical"] for r in good])
        if good
        else None,
        "dense_p95_ms": p95([r["channels"]["dense"] for r in good]) if good else None,
    }


def run(args):
    queries = [
        q
        for q in json.loads((args.corpus / "queries.json").read_text())
        if q["phase"] == "after-schema-change"
    ]
    judgments = json.loads((args.workload / "judgments.json").read_text())
    if len({q["id"] for q in queries}) != 408 or len(queries) != 408:
        raise ValueError("expected the full evolved workload")
    eligible = {q["id"]: set(judgments[q["id"]]["eligible"]) for q in queries}
    batches = list(islice(write_batches(args.corpus), 176))
    if len(batches) != 176:
        raise ValueError("incomplete frozen write workload")
    frozen = hashlib.sha256()
    for batch in batches:
        frozen.update(json.dumps(batch, separators=(",", ":"), sort_keys=True).encode())
    password = secrets.token_hex(24)
    admin = "admin:" + password
    os.environ["ARCHIVE_ANTFLY_AUTH"] = admin
    ep = port()
    url = f"http://127.0.0.1:{ep}"
    binary_sha = sha256_file(args.binary)
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = 65536 if hard == resource.RLIM_INFINITY else min(hard, 65536)
    if soft != resource.RLIM_INFINITY and soft < target:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    fds = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    with (args.out / "server.log").open("w") as log:
        proc = subprocess.Popen(
            [
                args.binary,
                "standalone",
                "--host",
                "127.0.0.1",
                "--port",
                str(ep),
                "--auth",
                "true",
                "--health",
                "false",
                "--data-dir",
                args.database,
            ],
            stdout=log,
            stderr=log,
            env=dict(os.environ, ANTFLY_BOOTSTRAP_ADMIN_PASSWORD=password),
        )
    try:
        for _ in range(120):
            try:
                tables = call(url + "/db/v1/tables", "GET", auth=admin)
                break
            except (OSError, RuntimeError) as exc:
                if proc.poll() is not None:
                    raise RuntimeError("startup failed") from exc
                time.sleep(1)
        else:
            raise TimeoutError("server startup")
        sources = [
            t
            for t in tables
            if t["name"].endswith("a") and t["schema"].get("index_sort")
        ]
        if len(sources) != 1:
            raise ValueError("expected one sorted A table")
        a = Antfly(url, sources[0]["name"], access="api_key")
        a.text_index = a.full_text_index(sources[0])
        a.create_readers()
        a.readers = {t: a.api_key(user.split(":")[0]) for t, user in a.readers.items()}
        deadline = time.monotonic() + 180
        probe = queries[283]
        while True:
            try:
                observations(
                    a,
                    probe,
                    "a",
                    "hybrid",
                    100,
                    20,
                    eligible[probe["id"]],
                    require_hits=True,
                )
                break
            except (RuntimeError, ValueError) as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "restored A did not serve the readiness probe"
                    ) from exc
                time.sleep(1)
        counts = wait_for_indexed_rows(a, 616832, "restored A")
        for q in queries[:4]:
            observations(a, q, "a", "hybrid", 100, 20, eligible[q["id"]])
        b = Antfly(url, "latencybackfillb", "write", access="api_key")
        b.create(384, evolved=True)
        time.sleep(
            30
        )  # Allow restored shard maintenance to settle before offering traffic.
        print("baseline", flush=True)
        baseline = traffic(a, queries, eligible, time.monotonic())
        publish(args.out / "baseline.json", baseline)
        print("backfill", flush=True)
        start = time.monotonic() + 0.1
        with ThreadPoolExecutor(max_workers=1) as pool:
            writer = pool.submit(backfill, b, batches, start, 1.2)
            loaded = traffic(a, queries, eligible, start)
            writes = writer.result()
        b_counts = wait_for_indexed_rows(b, 176 * 128, "paced B")
        timings = [r["scheduled"] + r.get("latency", 0) for r in loaded]
        overlapped = sum(
            "digest" in r and r["scheduled"] + r["latency"] <= writes[-1][1]
            for r in loaded
        )
        publish(args.out / "during.json", loaded)
        publish(args.out / "write-times.json", writes)
        publish(
            args.out / "summary.json",
            dict(
                binary_sha256=binary_sha,
                nofile_soft=fds,
                a_counts=counts,
                b_counts=b_counts,
                baseline=summarize(baseline),
                during=summarize(loaded),
                write_batches=len(writes),
                last_write_seconds=writes[-1][1],
                last_read_seconds=max(timings),
                completed_before_last_write=overlapped,
                latest_write_dispatch_lag_ms=round(
                    1000 * max(sent - i * 1.2 for i, (sent, _) in enumerate(writes)), 3
                ),
                baseline_during_rank_changes=sum(
                    r["digest"] != s["digest"]
                    for r, s in zip(baseline, loaded, strict=True)
                    if "digest" in r and "digest" in s
                ),
                baseline_during_comparable=sum(
                    "digest" in r and "digest" in s
                    for r, s in zip(baseline, loaded, strict=True)
                ),
                queries_sha256=sha256_file(args.corpus / "queries.json"),
                judgments_sha256=sha256_file(args.workload / "judgments.json"),
                b_ids_sha256=hashlib.sha256(
                    "".join(row["id"] for batch in batches for row in batch).encode()
                ).hexdigest(),
                b_input_sha256=frozen.hexdigest(),
            ),
        )
    finally:
        stop(proc)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("corpus", "workload", "database", "binary", "out"):
        p.add_argument("--" + name, type=Path, required=True)
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    run(args)
