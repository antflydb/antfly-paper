"""Produce an aggregate Markdown report from completed full-corpus migration receipts."""

import argparse
import hashlib
import json
import math
from pathlib import Path


def traffic(rows):
    latencies = sorted(
        r["latency"] for r in rows if "latency" in r and "error" not in r
    )
    return {
        "arrivals": len(rows),
        "errors": sum("error" in row for row in rows),
        "overload": sum(
            row.get("error") == "load generator at concurrency limit" for row in rows
        ),
        "ineligible": sum(row.get("ineligible", 0) for row in rows),
        "p95_ms": round(latencies[math.ceil(0.95 * len(latencies)) - 1] * 1000)
        if latencies
        else None,
    }


def summarize(path):
    data = path.read_bytes()
    run = json.loads(data)
    active = traffic(run["traffic"])
    counts = run["b_indexed_counts"]
    if not counts or any(v != run["rows_per_generation"] for v in counts.values()):
        raise ValueError("incomplete B coverage")
    metrics = run["model_metrics"]
    if metrics["embedded_chunks"] != run["source"]["chunks"]:
        raise ValueError("expected a complete inference-inclusive migration")
    return {
        "engine": run["engine"],
        "result_sha256": hashlib.sha256(data).hexdigest(),
        "rows": run["rows_per_generation"],
        "b_ready_s": run["b_ready_seconds"],
        "cutover_s": run["cutover_seconds"],
        "baseline": traffic(run["baseline_traffic"]),
        "traffic": active,
        "scheduled": run["scheduled_arrivals"],
        "missing": max(0, run["scheduled_arrivals"] - active["arrivals"]),
        "rankings": {
            key: {
                k: cell["quality"][k]
                for k in ("queries", "recall", "complete", "ineligible")
            }
            for key, cell in run["results"].items()
            if "quality" in cell
        },
    }, (
        run["source"],
        run["k"],
        run["depth"],
        run["qps"],
        run["migration"],
        run.get("readiness_probe_ordinal", 1),
    )


def report(paths, cohort):
    trials = []
    config = None
    for path in paths:
        trial, key = summarize(path)
        if config is not None and key != config:
            raise ValueError(
                "input corpus, query budget, load or migration mode differs"
            )
        config = key
        trials.append(trial)
    if len(set(t["engine"] for t in trials)) != len(trials):
        raise ValueError("supply one receipt per engine per cohort")
    source, k, depth, qps, _, probe = config
    lines = [
        "# OfficeQA Pro v2 migration results",
        "",
        cohort,
        "",
        f"{source['chunks']:,} distinct chunks per trial. Candidate depth {depth}, page budget {k}, offered load {qps:g} queries/s, readiness probe ordinal {probe}.",
        "",
        "| Engine | B ready (min) | Client cutover (ms) | A p95 before / during (ms) | Scheduled | Missing | Errors (incl. overload) | Ineligible pages during traffic |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for t in trials:
        active = t["traffic"]
        lines.append(
            f"| {t['engine']} | {t['b_ready_s'] / 60:.2f} | {1000 * t['cutover_s']:.0f} | {t['baseline']['p95_ms']} / {active['p95_ms']} | {t['scheduled']:,} | {t['missing']} | {active['errors']} | {active['ineligible']} |"
        )
    lines.extend(
        [
            "",
            f"| Evolved schema | Engine | Variants | Mean page recall@{k} | All cited pages@{k} | Ineligible pages |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for generation in ("a", "b"):
        for channel in ("lexical", "dense", "hybrid"):
            for t in trials:
                c = t["rankings"][f"{generation}-after-schema-change-{channel}"]
                lines.append(
                    f"| {generation.upper()} {channel} | {t['engine']} | {c['queries']} | {c['recall']:.2%} | {c['complete']:.2%} | {c['ineligible']} |"
                )
    lines.extend(
        [
            "",
            "Latency p95 covers completed successful requests. Errors and missed arrivals are counted separately. Source receipt SHA-256 digests follow.",
            "",
        ]
    )
    lines.extend(f"- {t['engine']} `{t['result_sha256']}`" for t in trials)
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("receipts", type=Path, nargs="+")
    p.add_argument(
        "--cohort", required=True, help="platform and deployment shared by these trials"
    )
    args = p.parse_args()
    print(report(args.receipts, args.cohort), end="")
