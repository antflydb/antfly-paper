"""Isolate credential and filter costs on a public workload with an exhaustive membership oracle."""

import argparse
import json
import math
import os
import random
import secrets
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from compare import wait_for_indexed_rows
from engines import Antfly, OpenSearch, call
from qualify import ANT, OS, ROOT

ROWS = 4096
DIM = 384
BUCKETS = 64
MODES = ("antfly_policy", "antfly_trusted", "opensearch_trusted", "opensearch_policy")


def summarize(values):
    ordered = sorted(values)
    return {
        k: round(1000 * ordered[math.ceil(p * len(ordered)) - 1], 3)
        for k, p in (("p50_ms", 0.5), ("p95_ms", 0.95))
    } | {"n": len(values)}


def wait_for_subject(mode):
    ant = mode.startswith("antfly")
    base = "http://127.0.0.1:" + ("8080" if ant else "9200")
    for _ in range(120):
        try:
            call(
                base
                + (
                    "/db/v1/tables"
                    if ant
                    else "/_cluster/health?wait_for_status=yellow"
                ),
                "GET",
                auth=os.getenv("ARCHIVE_ANTFLY_AUTH" if ant else "ARCHIVE_OS_AUTH"),
            )
            break
        except (OSError, RuntimeError):
            time.sleep(1)
    else:
        raise TimeoutError("readiness")
    return base


def trial(mode):
    rng = random.Random(731)
    rows = []
    for i in range(ROWS):
        rows.append(
            {
                "id": f"r{i}",
                "page": f"p{i}.pdf#page=1",
                "tenant": ("archive", "audit")[i % 2],
                "retention_years": (i // 2) % BUCKETS,
                "body": "Treasury ledger fiscal receipts",
                "vec": [rng.uniform(-1, 1) for _ in range(DIM)],
            }
        )
    protected = mode == "antfly_policy"
    ant = mode.startswith("antfly")
    base = wait_for_subject(mode)
    if ant:
        e = Antfly(
            base, "fairness", "write", access="basic" if protected else "application"
        )
        e.create_indexes(DIM, evolved=True)
        if protected:
            e.create_readers()
    else:
        e = OpenSearch(base, "fairness", policy=mode == "opensearch_policy")
        e.create(DIM)
        e.evolve()
    credentials = (
        ("basic", "api_key")
        if protected
        else (("basic",) if mode == "opensearch_policy" else ("application",))
    )
    if protected:
        basic_writer = e.admin
        basic_readers = dict(e.readers)
        keys = {t: e.api_key(login.split(":")[0]) for t, login in basic_readers.items()}
        writer_key = e.api_key(basic_writer.split(":")[0])
    groups = {c: {"write_batch": [], "lexical": [], "dense": []} for c in credentials}
    for batch, i in enumerate(range(0, ROWS, 128)):
        cred = credentials[(batch // 2 + batch) % len(credentials)]
        if protected:
            e.admin = basic_writer if cred == "basic" else writer_key
        started = time.perf_counter()
        e.insert(rows[i : i + 128])
        groups[cred]["write_batch"].append(time.perf_counter() - started)
    e.finish()
    wait_for_indexed_rows(e, ROWS, "fixture")
    minimum_recall = {c: 1.0 for c in credentials}
    for cycle in range(5):
        for j in range(16):
            tenant = ("archive", "audit")[j % 2]
            bucket = (7 * j) % BUCKETS
            chosen = next(
                r
                for r in rows
                if r["tenant"] == tenant and r["retention_years"] == bucket
            )
            expected = {
                r["id"]
                for r in rows
                if r["tenant"] == tenant and r["retention_years"] == bucket
            }
            q = {
                "tenant": tenant,
                "question": "fiscal receipts",
                "filter": {"field": "retention_years", "equals": bucket},
            }
            for channel in ("lexical", "dense"):
                for cred in credentials if (cycle + j) % 2 == 0 else credentials[::-1]:
                    if protected:
                        e.readers[tenant] = (
                            basic_readers[tenant] if cred == "basic" else keys[tenant]
                        )
                    start = time.perf_counter()
                    hits = e.search(q, chosen["vec"], channel, 100)
                    duration = time.perf_counter() - start
                    ids = {h[0] for h in hits}
                    if len(ids) != len(hits) or ids - expected:
                        raise ValueError("duplicate or ineligible hits")
                    minimum_recall[cred] = min(
                        minimum_recall[cred], len(ids) / len(expected)
                    )
                    if cycle:
                        groups[cred][channel].append(duration)
    return {
        "minimum_membership_recall": minimum_recall,
        "timing": {
            c: {kind: summarize(values) for kind, values in timings.items()}
            for c, timings in groups.items()
        },
    }


@contextmanager
def subject(mode, mounts=()):
    secret = "Ac!" + secrets.token_hex(24)
    env = dict(
        os.environ,
        ANTFLY_BOOTSTRAP_ADMIN_PASSWORD=secret,
        OPENSEARCH_INITIAL_ADMIN_PASSWORD=secret,
        ARCHIVE_OS_AUTH="admin:" + secret,
        ARCHIVE_ANTFLY_AUTH="admin:" + secret,
    )
    cmd = [
        "docker",
        "run",
        "--rm",
        "-d",
        "--network",
        "none",
        "--cpus",
        "4",
        "--memory",
        "4g",
    ]
    if mode.startswith("opensearch"):
        cmd += [
            "-e",
            "discovery.type=single-node",
            *(
                [
                    "-e",
                    "OPENSEARCH_INITIAL_ADMIN_PASSWORD",
                    "-e",
                    "plugins.security.ssl.http.enabled=false",
                ]
                if mode == "opensearch_policy"
                else [
                    "-e",
                    "DISABLE_SECURITY_PLUGIN=true",
                    "-e",
                    "DISABLE_INSTALL_DEMO_CONFIG=true",
                ]
            ),
            "-e",
            "OPENSEARCH_JAVA_OPTS=-Xms1g -Xmx1g",
            OS,
        ]
    else:
        cmd += [
            "-e",
            "ANTFLY_BOOTSTRAP_ADMIN_PASSWORD",
            ANT,
            "standalone",
            "--host",
            "0.0.0.0",
            "--port",
            "8080",
            "--auth",
            str(mode in ("antfly_policy", "antfly_basic")).lower(),
            "--health",
            "false",
            "--data-dir",
            "/tmp/fairness",
        ]
    server = subprocess.check_output(cmd, env=env, text=True).strip()
    client = [
        "docker",
        "run",
        "--rm",
        "--network",
        "container:" + server,
        "-v",
        f"{ROOT}:/paper:ro",
        *(
            ["-e", "ARCHIVE_ANTFLY_AUTH"]
            if mode in ("antfly_policy", "antfly_basic")
            else (["-e", "ARCHIVE_OS_AUTH"] if mode == "opensearch_policy" else [])
        ),
        "--entrypoint",
        "python",
        "python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b",
    ]
    client[3:3] = mounts
    try:
        yield client, env
    finally:
        subprocess.run(
            ["docker", "stop", "-t", "2", server], check=True, stdout=subprocess.DEVNULL
        )


def campaign(out):
    if out.exists():
        raise FileExistsError(out)
    runs = []
    for round_no in range(len(MODES)):
        for mode in MODES[round_no:] + MODES[:round_no]:
            with subject(mode) as (client, env):
                run = subprocess.check_output(
                    [
                        *client,
                        "/paper/evaluations/archive/fairness_control.py",
                        "--trial",
                        mode,
                    ],
                    env=env,
                    text=True,
                )
            runs.append({"round": round_no + 1, "mode": mode, **json.loads(run)})
            print(f"completed round {round_no + 1} {mode}", flush=True)
    out.write_text(
        json.dumps(
            {
                "rows": ROWS,
                "dimension": DIM,
                "eligible_rows_per_query": ROWS // 2 // BUCKETS,
                "candidate_depth": 100,
                "server_cpus": 4,
                "server_memory_gib": 4,
                "antfly_image": ANT,
                "opensearch_image": OS,
                "runs": runs,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--trial", choices=MODES)
    g.add_argument("--out", type=Path)
    args = p.parse_args()
    if args.trial:
        print(json.dumps(trial(args.trial)))
    else:
        campaign(args.out)
