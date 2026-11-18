"""Drive native index and durable-outbox generation builds over the identical archive chunks."""

import argparse
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path

# corpus imports the public document application's shared model and PDF frontend.
sys.path.append(str(Path(__file__).resolve().parents[1] / "head-to-head/scripts"))
from adapters import Antfly, Postgres  # noqa: E402
from corpus import EMBEDDER, MIGRATION_EMBEDDER, chunk_rows, embed  # noqa: E402


def served_distance(app, key, tenant, text, vector):
    body = {
        "table": "documents",
        "indexes": ["b1"],
        "embeddings": {"b1": vector},
        "filter_query": {"ids": [key]},
        "limit": 1,
        "fields": ["tenant", "body"],
    }
    answer = app.api("/db/v1/query", body, tenant=tenant)["responses"][0]
    hit = answer["hits"]["hits"][0]
    if (
        hit["_id"] != key
        or hit["_source"]["body"] != text
        or "b1" not in answer["resolved_indexes"]
    ):
        raise ValueError("incorrect native B source or index")
    return hit["_distance"]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("engine", choices=["antfly", "postgres"])
    p.add_argument(
        "--endpoint",
        required=True,
        help="fresh Antfly standalone or durable-outbox application",
    )
    p.add_argument(
        "--models", required=True, help="inference URL reachable by the controller"
    )
    p.add_argument(
        "--producer-models", help="same model service as reached from Antfly"
    )
    p.add_argument("--corpus", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--deadline", default=86400, type=float)
    p.add_argument("--qps", default=2.0, type=float)
    p.add_argument(
        "--max-chunks",
        type=int,
        help="bounded qualification subset in frozen corpus order",
    )
    p.add_argument(
        "--gate",
        help="controlled model gateway for a concurrent source and permission correction",
    )
    args = p.parse_args()
    if (
        args.deadline <= 0
        or not 0 < args.qps <= 100
        or (args.max_chunks is not None and args.max_chunks < 1)
    ):
        p.error("invalid workload limits")
    manifest = json.loads((args.corpus / "manifest.json").read_text())
    args.out.mkdir(parents=True, exist_ok=False)
    from common import request

    gate_headers = {"Authorization": "Bearer " + os.environ.get("PAPER_TOKEN", "")}
    validation_inputs = set()
    begin = (
        len(request(args.gate + "/audit", headers=gate_headers)["calls"])
        if args.gate
        else 0
    )
    if args.engine == "antfly":
        app = Antfly(
            args.endpoint,
            os.environ["PAPER_PASSWORD"],
            model_url=args.producer_models or args.models,
        )
    else:
        app = Postgres(
            args.endpoint, os.environ["PAPER_TOKEN"], os.environ["PAPER_PASSWORD"]
        )
    if args.gate and args.engine == "antfly":
        calls = request(args.gate + "/audit", headers=gate_headers)["calls"][begin:]
        validation_inputs = {
            c["input_sha256"]
            for c in calls
            if c["task"] == "embed" and c.get("status") == 200
        }
        if not validation_inputs or None in validation_inputs:
            raise RuntimeError("missing pre-ingest index admission probe")
    import itertools

    counts = 0
    tenants = {}
    for chunk in itertools.islice(chunk_rows(args.corpus), args.max_chunks):
        key = hashlib.sha256(chunk["id"].encode()).hexdigest()
        tenant = "alpha" if "archive" in chunk["views"] else "beta"
        row = {
            "tenant": tenant,
            "body": chunk["body"],
            "version": 1,
            "page": int(chunk["page"].rsplit("=", 1)[1]),
        }
        app.write(key, row)
        tenants[key] = tenant
        counts += 1
    if not counts:
        raise ValueError("empty corpus")
    # Synthetic sentinels have known source content and coexist with the same corpus in both subjects.
    sentinels = {
        "probe-alpha": "archive qualification lamp canon",
        "probe-beta": "archive qualification rope canon",
    }
    for key, body in sentinels.items():
        tenant = key.split("-")[-1]
        tenants[key] = tenant
        app.write(key, {"tenant": tenant, "body": body, "page": 1, "version": 1})
    count = counts + len(sentinels)

    def complete(c):
        return (
            c.get("observation_complete") is True
            and c.get("complete") is True
            and c["source_total"] == c["produced"] == count
        )

    def wait_complete(g):
        until = time.monotonic() + args.deadline
        while not complete(app.completion(g)):
            if time.monotonic() > until:
                raise TimeoutError("coverage for " + g)
            time.sleep(0.5)

    wait_complete("a0")
    alias = "/db/v1/tables/documents/index-aliases/archive_live"

    def activate(payload):
        until = time.monotonic() + args.deadline
        while True:
            try:
                return app.api(alias, payload, method="PUT")
            except OSError as exc:
                if "IndexAliasTargetNotReady" not in str(exc):
                    raise
                if time.monotonic() >= until:
                    status = app.api(
                        "/db/v1/tables/documents/indexes/" + payload["index"]
                    )["status"]["readiness"]
                    raise TimeoutError(f"alias admission: {status}") from exc
                time.sleep(0.25)

    if args.engine == "antfly":
        activate(
            {"index": "a0", "require": "complete", "idempotency_key": "archive-a"},
        )
    if args.gate:
        request(
            args.gate + "/control",
            {"hold": True, "allow_input_sha256": sorted(validation_inputs)},
            headers=gate_headers,
        )
    start = time.monotonic()
    events = {}
    try:
        app.generation("b1", MIGRATION_EMBEDDER)
        if args.gate:
            until = time.monotonic() + 90
            while not request(args.gate + "/held-requests", headers=gate_headers)[
                "pending"
            ]:
                if time.monotonic() > until:
                    raise TimeoutError("no observed B work held")
                time.sleep(0.05)
            events["b_request_held"] = time.monotonic() - start
            old = embed([sentinels["probe-alpha"]], args.models, EMBEDDER)[0]
            served = app.query("alpha", old, "a0")
            if not served["available"] or not any(
                h["id"] == "probe-alpha" for h in served["hits"]
            ):
                raise ValueError("A failed the positive control during held B work")
            sentinels["probe-alpha"] = "corrected archive qualification receiver ledger"
            tenants["probe-alpha"] = "beta"
            app.write(
                "probe-alpha",
                {
                    "body": sentinels["probe-alpha"],
                    "version": 2,
                    "page": 1,
                    "tenant": "beta",
                },
            )
            events["correction_ack"] = time.monotonic() - start
            restricted = app.query("alpha", old, "a0")
            if any(h["id"] == "probe-alpha" for h in restricted["hits"]):
                raise ValueError(
                    "restricted old-generation path exposed the corrected source"
                )
            events["restricted_reader_available"] = restricted["available"]
    finally:
        if args.gate:
            request(args.gate + "/control", {"hold": False}, headers=gate_headers)
            events["release"] = time.monotonic() - start
    probes = json.loads((args.corpus / "queries.json").read_text())
    probes = [q for q in probes if q["phase"] == "original"]
    if not probes:
        raise ValueError("missing original query vectors")
    progress = []
    i = 0
    with (args.out / "traffic.jsonl").open("w") as out:
        while True:
            t = time.monotonic() - start
            if t > args.deadline:
                raise TimeoutError("managed generation B")
            c = app.completion("b1")
            progress.append((t, c.get("produced")))
            if complete(c):
                break
            due = start + i / args.qps
            time.sleep(max(0, due - time.monotonic()))
            q = probes[i % len(probes)]
            tenant = "alpha" if i % 2 == 0 else "beta"
            try:
                result = app.query(
                    tenant, embed([q["question"]], args.models, EMBEDDER)[0], "a0"
                )
                observation = {
                    "available": result["available"],
                    "hits": len(result["hits"]),
                    "ineligible": sum(
                        tenants.get(r["id"]) != tenant for r in result["hits"]
                    ),
                }
            except OSError as ex:
                observation = {"available": False, "error": str(ex)}
            out.write(
                json.dumps(
                    dict(
                        observation,
                        scheduled=i / args.qps,
                        latency=time.monotonic() - due,
                    )
                )
                + "\n"
            )
            out.flush()
            i += 1
    elapsed = time.monotonic() - start
    oracle = {}
    for key, body in sentinels.items():
        expected = embed([body], args.models, MIGRATION_EMBEDDER)[0]
        if args.engine == "antfly":
            # The v0.3.1 raw-inspection API covers protected projections only.
            # Probe the serving index with an independent vector and its antipode.
            near = served_distance(app, key, tenants[key], body, expected)
            far = served_distance(app, key, tenants[key], body, [-x for x in expected])
            if (
                not math.isfinite(near)
                or abs(near) > 0.001
                or not math.isfinite(far)
                or far < 1
            ):
                raise ValueError(
                    f"served native vector failed signed cosine probes: {near}, {far}"
                )
            oracle[key] = {"same_direction_distance": near, "antipode_distance": far}
            continue
        else:
            hits = app.query(tenants[key], expected, "b1", include_vectors=True)["hits"]
            observed = next(
                h["_embeddings"]["b1"]
                for h in hits
                if h["id"] == key and h["body"] == body
            )
        error = sum((a - b) ** 2 for a, b in zip(expected, observed, strict=True))
        if not math.isfinite(error) or error > 1e-5:
            raise ValueError("wrong stored B vector")
        oracle[key] = error
    if args.engine == "antfly":
        activate(
            {
                "index": "b1",
                "expected_index": "a0",
                "require": "complete",
                "idempotency_key": "archive-b",
            },
        )
        response = app.api(
            "/db/v1/query",
            {
                "table": "documents",
                "indexes": ["archive_live"],
                "semantic_search": sentinels["probe-alpha"],
                "limit": 1,
                "fields": ["tenant"],
            },
            tenant=tenants["probe-alpha"],
        )["responses"][0]
        if (
            "b1" not in response.get("resolved_indexes", [])
            or response["hits"]["hits"][0]["_id"] != "probe-alpha"
        ):
            raise ValueError("alias and query encoder cutover failed")
    stats = {
        "engine": args.engine,
        "source": manifest,
        "corpus_chunks": counts,
        "sentinels": len(sentinels),
        "completion_seconds": elapsed,
        "vector_oracle": oracle,
        "events": events,
        "coverage": c,
        "coverage_over_time": progress,
        "scheduled_arrivals": math.ceil(elapsed * args.qps),
        "recorded_arrivals": i,
        "model_a": EMBEDDER,
        "model_b": MIGRATION_EMBEDDER,
        "cutover": "native alias"
        if args.engine == "antfly"
        else "client selects B index and query encoder",
    }
    (args.out / "result.json").write_text(json.dumps(stats, indent=2))
    with (args.out / "traffic.jsonl").open() as stream:
        return int(any(json.loads(line).get("ineligible") for line in stream))


if __name__ == "__main__":
    raise SystemExit(main())
