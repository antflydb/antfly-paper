"""Replay one engine in an isolated deployment using identical page chunks and embeddings."""

import argparse
import hashlib
import json
import math
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from corpus import MIGRATION_EMBEDDER, chunk_rows, embed
from engines import Antfly, Lance, OpenSearch, Pg, Vespa
from evidence import fuse, score_pages


def observations(engine, query, gen, mode, depth, limit):
    channels = ("lexical", "dense") if mode == "hybrid" else (mode,)
    pages, ranks = {}, []
    for channel in channels:
        hits = engine.search(query, query[gen], channel, depth)
        pages.update(hits)
        ranks.append([key for key, _ in hits])
    chunks = fuse(ranks, depth * len(channels))
    return list(dict.fromkeys(pages[k] for k in chunks))[:limit]


def migration_rows(path, models, metrics):
    import itertools

    rows = iter(chunk_rows(path))
    while batch := list(itertools.islice(rows, 32)):
        if models:
            started = time.monotonic()
            vectors = embed([r["body"] for r in batch], models, MIGRATION_EMBEDDER)
            metrics["model_seconds"] += time.monotonic() - started
            metrics["model_batches"] += 1
            if len(vectors) != len(batch):
                raise ValueError("migration embedder dropped input")
            for row, v in zip(batch, vectors, strict=True):
                expected = row["b"]
                if len(v) != len(expected) or not all(math.isfinite(x) for x in v):
                    raise ValueError("invalid B vector")
                # Independent preparation supplies the expected direction for the same source.
                dot = sum(x * y for x, y in zip(v, expected, strict=True))
                norm = math.sqrt(sum(x * x for x in v) * sum(y * y for y in expected))
                if not norm or dot / norm < 0.999:
                    raise ValueError("migration model disagrees with frozen B vector")
                row["b"] = v
            metrics["embedded_chunks"] += len(batch)
        yield from batch


def load(
    engine, path, generation, evolved, *, models=None, metrics=None, progress=None
):
    batch = []
    count = 0
    batch_size = 2048 if isinstance(engine, Lance) else 128
    for chunk in migration_rows(path, models, metrics):
        for tenant, meta in {**chunk["views"], "public": {}}.items():
            identity = hashlib.sha256(
                (tenant + "\0" + chunk["id"]).encode()
            ).hexdigest()
            batch.append(
                dict(
                    id=identity,
                    page=chunk["page"],
                    tenant=tenant,
                    body=chunk["body"],
                    vec=chunk[generation],
                    **{
                        k: v
                        for k, v in meta.items()
                        if evolved or k != "retention_years"
                    },
                )
            )
            if len(batch) == batch_size:
                engine.insert(batch)
                count += len(batch)
                if progress:
                    progress(count)
                batch = []
    if batch:
        engine.insert(batch)
        count += len(batch)
    engine.finish()
    return count


def replay(engine, gen, queries, args, phase, gold):
    selected = [q for q in queries if q["phase"] == phase]
    if not selected:
        raise ValueError(f"no {phase} queries")
    by_mode = {}
    for mode in ("lexical", "dense", "hybrid"):
        rankings, times = {}, []
        for q in selected:
            start = time.monotonic()
            rankings[q["id"]] = observations(engine, q, gen, mode, args.depth, args.k)
            times.append(time.monotonic() - start)
        label = f"{gen}-{phase}-{mode}"
        (args.out / (label + ".json")).write_text(json.dumps(rankings))
        by_mode[label] = {
            "queries": len(times),
            "p95_seconds": sorted(times)[math.ceil(0.95 * len(times)) - 1],
        }
        if phase != "original":
            by_mode[label]["quality"] = score_pages(
                {q["id"]: gold[q["id"]] for q in selected}, rankings, args.k
            )
    return by_mode


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "engine", choices=("antfly", "postgres", "opensearch", "vespa", "lance")
    )
    p.add_argument(
        "--endpoint",
        required=True,
        help="fresh isolated deployment URL, PostgreSQL DSN or Lance directory",
    )
    for key in ("corpus", "workload", "out"):
        p.add_argument("--" + key, type=Path, required=True)
    p.add_argument("--qps", type=float, default=2)
    p.add_argument("--k", type=int, default=20)
    p.add_argument("--depth", type=int, default=100)
    p.add_argument(
        "--migration-models",
        help="rerun B through the pinned model endpoint during the timed build",
    )
    args = p.parse_args()
    if not 0 < args.k <= args.depth or not math.isfinite(args.qps) or args.qps <= 0:
        p.error("require positive qps and 0 < k <= depth")
    manifest = json.loads((args.corpus / "manifest.json").read_text())
    if "acquisition_sha256" in manifest:
        provenance = json.loads((args.workload / "provenance.json").read_text())
        if provenance["acquisition_sha256"] != manifest["acquisition_sha256"]:
            raise ValueError("workload acquisition differs from corpus")
    queries = json.loads((args.corpus / "queries.json").read_text())
    gold = json.loads((args.workload / "judgments.json").read_text())
    query_ids = [q["id"] for q in queries]
    if len(query_ids) != len(set(query_ids)) or gold.keys() != {
        q["id"] for q in queries if q["phase"] != "original"
    }:
        raise ValueError("corpus queries and workload judgments disagree")
    args.out.mkdir(parents=True, exist_ok=False)
    chunks = args.corpus
    first = next(chunk_rows(chunks))
    prefix = "archive" + uuid.uuid4().hex[:10]
    schemas = {}

    def build(g):
        if args.engine == "vespa":
            return Vespa(args.endpoint, prefix + g, schemas)
        return {
            "antfly": Antfly,
            "opensearch": OpenSearch,
            "lance": Lance,
            "postgres": Pg,
        }[args.engine](args.endpoint, prefix + g)

    a, b = build("a"), build("b")
    a.create(len(first["a"]))
    count = load(a, chunks, "a", False)
    results = replay(a, "a", queries, args, "initial", gold)
    a.evolve()
    if (
        a.populate_retention() if isinstance(a, Lance) else load(a, chunks, "a", True)
    ) != count:
        raise ValueError("schema evolution row count changed")
    results.update(replay(a, "a", queries, args, "after-schema-change", gold))
    results.update(replay(a, "a", queries, args, "original", gold))
    start = time.monotonic()
    probes = [q for q in queries if q["phase"] == "after-schema-change"]
    observed, stop = [], threading.Event()

    def traffic():
        i = 0
        while True:
            due = start + i / args.qps
            if stop.wait(max(0, due - time.monotonic())):
                break
            q = probes[i % len(probes)]
            try:
                hits = observations(a, q, "a", "hybrid", args.depth, args.k)
                quality = score_pages({q["id"]: gold[q["id"]]}, {q["id"]: hits}, args.k)
                observed.append(
                    dict(
                        arrival=i / args.qps,
                        latency=time.monotonic() - due,
                        id=q["id"],
                        hits=hits,
                        ineligible=quality["ineligible"],
                    )
                )
            except Exception as exc:
                observed.append(
                    dict(
                        arrival=i / args.qps,
                        latency=time.monotonic() - due,
                        error=str(exc),
                    )
                )
            i += 1

    model_metrics = {"model_batches": 0, "model_seconds": 0.0, "embedded_chunks": 0}
    progress = []
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(traffic)
        try:
            b.create(len(first["b"]))
            b.evolve()
            completed = load(
                b,
                chunks,
                "b",
                True,
                models=args.migration_models,
                metrics=model_metrics,
                progress=lambda count: progress.append(
                    [time.monotonic() - start, count]
                ),
            )
            if completed != count:
                raise ValueError("B row acknowledgment count mismatch")
            elapsed = time.monotonic() - start
        finally:
            stop.set()
            future.result()
    results.update(replay(b, "b", queries, args, "after-schema-change", gold))
    receipt = {
        "engine": args.engine,
        "source": manifest,
        "rows_per_generation": count,
        "k": args.k,
        "depth": args.depth,
        "qps": args.qps,
        "b_load_seconds": elapsed,
        "traffic": observed,
        "acknowledged_rows_over_time": progress + [[elapsed, completed]],
        "model_metrics": model_metrics,
        "scheduled_arrivals": math.ceil(elapsed * args.qps),
        "results": results,
        "migration": (
            "shared B inference, checked against frozen vectors"
            if args.migration_models
            else "precomputed B load"
        )
        + "; controller selects generation",
    }
    (args.out / "result.json").write_text(json.dumps(receipt, indent=2))
    missed = max(0, receipt["scheduled_arrivals"] - len(observed))
    return int(
        bool(
            missed
            or any(r.get("error") or r.get("ineligible") for r in observed)
            or any(r.get("quality", {}).get("ineligible") for r in results.values())
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
