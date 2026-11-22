"""Replay one engine in an isolated deployment using identical page chunks and embeddings."""

import argparse
import hashlib
import json
import math
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from corpus import MIGRATION_EMBEDDER, archive_embed, chunk_rows
from engines import Antfly, Lance, OpenSearch, Pg, Vespa
from evidence import fuse, score_pages


def observations(
    engine,
    query,
    gen,
    mode,
    depth,
    limit,
    eligible=None,
    channel_times=None,
    require_hits=False,
):
    channels = ("lexical", "dense") if mode == "hybrid" else (mode,)
    pages, ranks = {}, []
    for channel in channels:
        started = time.monotonic()
        hits = engine.search(query, query[gen], channel, depth)
        if channel_times is not None:
            channel_times[channel] = time.monotonic() - started
        if require_hits and not hits:
            raise ValueError(f"empty {channel} readiness probe")
        if eligible is not None and any(
            page.rsplit("#page=", 1)[0] not in eligible for _, page in hits
        ):
            raise ValueError(
                f"{channel} returned an ineligible candidate for {query['id']}"
            )
        pages.update(hits)
        ranks.append([key for key, _ in hits])
    chunks = fuse(ranks, depth * len(channels))
    return list(dict.fromkeys(pages[k] for k in chunks))[:limit]


def migration_rows(path, models, metrics, replacement):
    import itertools

    rows = iter(chunk_rows(path))
    while batch := list(itertools.islice(rows, 32)):
        if models:
            started = time.monotonic()
            vectors = archive_embed([r["body"] for r in batch], models, replacement)
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
    engine,
    path,
    generation,
    evolved,
    *,
    models=None,
    metrics=None,
    progress=None,
    replacement=MIGRATION_EMBEDDER,
    live_rows=None,
):
    batch = []
    count = 0
    batch_size = 2048 if isinstance(engine, Lance) else 128

    def write_batch(rows):
        start = time.monotonic()
        engine.insert(rows)
        if metrics is not None:
            metrics["write_call_seconds"] += time.monotonic() - start
            metrics["write_batches"] += 1

    for chunk in (
        live_rows
        if live_rows is not None
        else migration_rows(path, models, metrics, replacement)
    ):
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
                write_batch(batch)
                count += len(batch)
                if progress:
                    progress(count)
                batch = []
    if batch:
        write_batch(batch)
        count += len(batch)
        if progress:
            progress(count)
    return count


def wait_for_indexed_rows(engine, expected, generation):
    deadline = time.monotonic() + 900
    while True:
        counts = engine.indexed_counts()
        if counts and all(n == expected for n in counts.values()):
            return counts
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"{generation} coverage expected {expected}, observed {counts}"
            )
        time.sleep(1)


def replay(
    engine,
    gen,
    queries,
    args,
    phase,
    gold,
    *,
    modes=("lexical", "dense", "hybrid"),
    suffix="",
):
    selected = [q for q in queries if q["phase"] == phase]
    if not selected:
        raise ValueError(f"no {phase} queries")
    by_mode = {}
    for mode in modes:
        rankings, times = {}, []
        for q in selected:
            start = time.monotonic()
            rankings[q["id"]] = observations(
                engine,
                q,
                gen,
                mode,
                args.depth,
                args.k,
                set(gold[q["id"]]["eligible"]) if phase != "original" else None,
            )
            times.append(time.monotonic() - start)
        label = f"{gen}-{phase}-{mode}{suffix}"
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
    p.add_argument(
        "--original-only",
        action="store_true",
        help="build A and retrieve original-question pages for reader scoring",
    )
    p.add_argument(
        "--readiness-probe-ordinal",
        type=int,
        default=1,
        help="one-based evolved workload query, held fixed across a cohort",
    )
    p.add_argument(
        "--access",
        choices=("default", "api_key", "application", "dls"),
        default="default",
    )
    p.add_argument(
        "--postgres-lexical-control",
        action="store_true",
        help="replay A with disjunctive English lexemes after timing",
    )
    p.add_argument("--application-reader")
    p.add_argument("--pdf-models")
    p.add_argument("--pdf-refs", type=Path)
    p.add_argument("--replay-catalog", type=Path)
    args = p.parse_args()
    if args.postgres_lexical_control and (
        args.engine != "postgres" or args.original_only
    ):
        p.error("lexical control requires a full PostgreSQL trial")
    supported = {"antfly": {"api_key", "application"}, "opensearch": {"dls"}}
    if args.access != "default" and args.access not in supported.get(
        args.engine, set()
    ):
        p.error("access mode is incompatible with the selected engine")
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
    pdf_migration = manifest.get("representations") == "pdf-images-to-passages-v1"
    if pdf_migration and (
        args.migration_models or args.original_only or args.postgres_lexical_control
    ):
        p.error(
            "the representation comparison uses two frozen inputs and a dense-only A"
        )
    a_inputs = args.corpus / "a" if pdf_migration else args.corpus
    b_inputs = args.corpus / "b" if pdf_migration else args.corpus
    a_dim = len(next(chunk_rows(a_inputs))["a"])
    b_dim = len(next(chunk_rows(b_inputs))["b"]) if not args.original_only else None
    a_modes = ("dense",) if pdf_migration else ("lexical", "dense", "hybrid")
    traffic_mode = "dense" if pdf_migration else "hybrid"
    prefix = "archive" + uuid.uuid4().hex[:10]
    schemas = {}

    def build(g):
        if args.engine == "antfly":
            return Antfly(
                args.endpoint,
                prefix + g,
                "write" if pdf_migration or g == "b" else "full_index",
                access="basic" if args.access == "default" else args.access,
            )
        if args.engine == "opensearch":
            return OpenSearch(args.endpoint, prefix + g, policy=args.access == "dls")
        if args.engine == "vespa":
            return Vespa(args.endpoint, prefix + g, schemas)
        return {
            "lance": Lance,
            "postgres": Pg,
        }[args.engine](args.endpoint, prefix + g)

    a, b = build("a"), build("b")
    a_started = time.monotonic()
    if pdf_migration and isinstance(a, Antfly):
        a.create(a_dim, evolved=True)
    else:
        a.create(a_dim)
        if pdf_migration:
            a.evolve()
    from pdf_pipeline import live_rows

    phase_costs = {"a": Counter(), "b": Counter()}

    def source(g):
        if args.pdf_models:
            if not pdf_migration or not args.pdf_refs:
                raise ValueError("live PDF migration requires pinned sources")
            return live_rows(
                g,
                args.pdf_models,
                args.pdf_refs,
                args.replay_catalog,
                manifest,
                args.workload,
                phase_costs[g],
                args.application_reader,
            )
        return None

    count = load(a, a_inputs, "a", pdf_migration, live_rows=source("a"))
    a.finish()
    if pdf_migration:
        if count != manifest["counts"]["a_rows"]:
            raise ValueError("page-image row count disagrees with the frozen inventory")
        wait_for_indexed_rows(a, count, "A")
    a_build_seconds = time.monotonic() - a_started
    if args.original_only:
        wait_for_indexed_rows(a, count, "A")
        results = replay(a, "a", queries, args, "original", gold)
        (args.out / "result.json").write_text(
            json.dumps(
                {
                    "engine": args.engine,
                    "source": manifest,
                    "rows_per_generation": count,
                    "k": args.k,
                    "depth": args.depth,
                    "results": results,
                },
                indent=2,
            )
        )
        return
    results = replay(a, "a", queries, args, "initial", gold, modes=a_modes)
    if not pdf_migration:
        a.evolve()
        if (
            a.populate_retention()
            if isinstance(a, Lance)
            else load(a, a_inputs, "a", True)
        ) != count:
            raise ValueError("schema evolution row count changed")
        if not isinstance(a, Lance):
            a.finish()
    results.update(
        replay(a, "a", queries, args, "after-schema-change", gold, modes=a_modes)
    )
    results.update(replay(a, "a", queries, args, "original", gold, modes=a_modes))
    probes = [q for q in queries if q["phase"] == "after-schema-change"]
    if not 1 <= args.readiness_probe_ordinal <= len(probes):
        raise ValueError("readiness probe outside evolved workload")
    probe = probes[args.readiness_probe_ordinal - 1]
    for channel in ("dense",) if pdf_migration else ("lexical", "dense"):
        a_ranks = json.loads(
            (args.out / f"a-after-schema-change-{channel}.json").read_text()
        )
        if not a_ranks[probe["id"]]:
            raise ValueError(f"chosen readiness probe already empty on A ({channel})")
    eligible = {q["id"]: set(gold[q["id"]]["eligible"]) for q in probes}
    stop = threading.Event()
    start = time.monotonic()

    def serve(i):
        q = probes[i % len(probes)]
        dispatched = time.monotonic() - start
        stages = {}
        row = dict(
            arrival=i / args.qps,
            dispatched=dispatched,
            id=q["id"],
            channel_seconds=stages,
        )
        try:
            row["hits"] = observations(
                a, q, "a", traffic_mode, args.depth, args.k, eligible[q["id"]], stages
            )
            row["ineligible"] = score_pages(
                {q["id"]: gold[q["id"]]}, {q["id"]: row["hits"]}, args.k
            )["ineligible"]
        except Exception as exc:
            row["error"] = str(exc)
        row["completed"] = time.monotonic() - start
        row["latency"] = row["completed"] - row["arrival"]
        return row

    def traffic(limit=None):
        observed, pending = [], []
        with ThreadPoolExecutor(max_workers=8) as workers:
            i = 0
            while limit is None or i < limit:
                if stop.wait(max(0, start + i / args.qps - time.monotonic())):
                    break
                for job in pending.copy():
                    if job.done():
                        observed.append(job.result())
                        pending.remove(job)
                if len(pending) < 8:
                    pending.append(workers.submit(serve, i))
                else:
                    observed.append(
                        {
                            "arrival": i / args.qps,
                            "error": "load generator at concurrency limit",
                        }
                    )
                i += 1
            observed.extend(job.result() for job in pending)
        return sorted(observed, key=lambda row: row["arrival"])

    baseline = traffic(len(probes))
    start = time.monotonic()
    model_metrics = {
        "model_batches": 0,
        "model_seconds": 0.0,
        "embedded_chunks": 0,
        "write_batches": 0,
        "write_call_seconds": 0.0,
    }
    progress = []
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(traffic)
        try:
            if isinstance(b, Antfly):
                b.create(b_dim, evolved=True)
            else:
                b.create(b_dim)
                b.evolve()
            completed = load(
                b,
                b_inputs,
                "b",
                True,
                models=args.migration_models,
                live_rows=source("b"),
                metrics=model_metrics,
                replacement=manifest.get("embedding_b", MIGRATION_EMBEDDER),
                progress=lambda count: progress.append(
                    [time.monotonic() - start, count]
                ),
            )
            expected_b = manifest["counts"]["b_rows"] if pdf_migration else count
            if completed != expected_b:
                raise ValueError("B row acknowledgment count mismatch")
            writes = time.monotonic() - start
            b.finish()
            counts = wait_for_indexed_rows(b, expected_b, "B")
            observations(
                b,
                probe,
                "b",
                traffic_mode,
                args.depth,
                args.k,
                eligible[probe["id"]],
                require_hits=True,
            )
            elapsed = time.monotonic() - start
        finally:
            stop.set()
            observed = future.result()
    switch = time.monotonic()
    active, encoder = b, "b"
    first_hits = observations(
        active,
        probe,
        encoder,
        traffic_mode,
        args.depth,
        args.k,
        eligible[probe["id"]],
        require_hits=True,
    )
    cutover = time.monotonic() - switch
    results.update(replay(b, "b", queries, args, "after-schema-change", gold))
    receipt = {
        "engine": args.engine,
        "source": manifest,
        "rows_per_generation": count,
        "k": args.k,
        "depth": args.depth,
        "qps": args.qps,
        "b_load_seconds": elapsed,
        "b_writes_complete_seconds": writes,
        "b_ready_seconds": elapsed,
        "b_indexed_counts": counts,
        "cutover_request_seconds": switch - start,
        "cutover_complete_seconds": switch - start + cutover,
        "cutover_seconds": cutover,
        "access": args.access,
        "readiness_probe_ordinal": args.readiness_probe_ordinal,
        "cutover_probe": {"id": probe["id"], "pages": first_hits, "encoder": encoder},
        "migration_write_barrier": "write"
        if args.engine == "antfly"
        else "adapter default",
        "baseline_traffic": baseline,
        "traffic_concurrency": 8,
        "traffic": observed,
        "acknowledged_rows_over_time": progress,
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
    if args.pdf_models:
        receipt["migration"] = (
            "live remote PDF fetch, extraction, chunking, embedding and indexing; application cutover"
        )
        receipt["application_stage_counts"] = phase_costs
    if pdf_migration:
        receipt.pop("rows_per_generation")
        receipt.update(
            a_rows=count,
            b_rows=completed,
            a_build_seconds=a_build_seconds,
            serving_channel=traffic_mode,
        )
        results.update(replay(b, "b", queries, args, "original", gold))
    if args.postgres_lexical_control:
        a.match_any_lexeme = True
        receipt["postgres_lexical_sensitivity"] = replay(
            a,
            "a",
            queries,
            args,
            "after-schema-change",
            gold,
            modes=("lexical", "hybrid"),
            suffix="-any-lexeme",
        )
    if isinstance(b, Antfly):
        receipt["antfly_evolved_schema"] = b.schema(evolved=True)
    (args.out / "result.json").write_text(json.dumps(receipt, indent=2))
    missed = max(0, receipt["scheduled_arrivals"] - len(observed))
    return int(
        bool(
            missed
            or any(r.get("error") or r.get("ineligible") for r in baseline + observed)
            or any(r.get("quality", {}).get("ineligible") for r in results.values())
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
