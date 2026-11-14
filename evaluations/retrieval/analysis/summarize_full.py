"""Distill checksum-verified full retrieval lifecycles; never fill missing runs."""

import argparse
import hashlib
import json
import statistics
from pathlib import Path

ENGINES = ("antfly", "pgvector", "pgvector-partial", "paradedb", "lancedb")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def spread(values):
    return (
        {"mean": statistics.mean(values), "min": min(values), "max": max(values)}
        if values
        else None
    )


def collect(root):
    records, sources, failures = {}, {}, []
    for ordinal in range(1, 4):
        for engine in ENGINES:
            path = root / f"{ordinal:02d}-{engine}"
            if not (path / "receipt.json").exists():
                failures.append(
                    {
                        "engine": engine,
                        "lifecycle": ordinal,
                        "status": "missing-or-active",
                    }
                )
                continue
            receipt = json.loads((path / "receipt.json").read_text())
            for name, expected in receipt["sha256"].items():
                if digest(path / name) != expected:
                    raise ValueError("receipt mismatch: " + str(path / name))
            result = json.loads((path / "result.json").read_text())
            sources[path.name] = {
                "result_sha256": digest(path / "result.json"),
                "receipt_sha256": digest(path / "receipt.json"),
            }
            if result["status"] != "completed":
                failures.append(
                    {
                        "engine": engine,
                        "lifecycle": ordinal,
                        "status": result["status"],
                        "error_type": result.get("error_type"),
                        "reason": result.get("reason"),
                    }
                )
                continue
            if (
                result["calibration_queries"] != list(range(20))
                or result["holdout_queries"] != list(range(20, 120))
                or not result["selection_frozen_before_holdout"]
            ):
                raise ValueError("full protocol partition mismatch")
            records.setdefault(engine, []).append(result)
    summary = {
        "completed_lifecycles": sum(map(len, records.values())),
        "requested_lifecycles": 15,
        "missing_or_failed": failures,
        "sources": sources,
        "profiles": {},
    }
    for engine, runs in records.items():
        cells = {}
        for cell in runs[0]["cells"]:
            observations = [run["cells"][cell] for run in runs]
            passing = len(observations) == 3 and all(
                c["status"] == "quality-passed" for c in observations
            )
            holds = [c["holdout"] for c in observations if "holdout" in c]
            exact = [c["exact_control"] for c in observations if "exact_control" in c]
            all_measures = [
                trial["aggregate"] for c in observations for trial in c["calibration"]
            ]
            all_measures.extend(
                c[key]
                for c in observations
                for key in (
                    "holdout",
                    "exact_control",
                    "global_top10_postfilter_ablation",
                )
                if key in c
            )
            cells[cell] = {
                "lifecycles": len(observations),
                "matched_quality_across_three": passing,
                "statuses": [c["status"] for c in observations],
                "selected": [c["selected"] for c in observations],
                "holdout_recall_at_10": spread([h["recall_at_10"] for h in holds]),
                "holdout_queries": sum(h["queries"] for h in holds),
                "holdout_p50_ms": spread([h["latency_ms"]["p50"] for h in holds])
                if passing
                else None,
                "holdout_p95_ms": spread([h["latency_ms"]["p95"] for h in holds])
                if passing
                else None,
                "exact_recall_at_10": spread([e["recall_at_10"] for e in exact]),
                "errors": sum(a["errors"] for a in all_measures),
                "unauthorized": sum(a["unauthorized"] for a in all_measures),
                "duplicates": sum(a["duplicates"] for a in all_measures),
                "postfilter_completeness": spread(
                    [
                        c["global_top10_postfilter_ablation"]["completeness"]
                        for c in observations
                        if "global_top10_postfilter_ablation" in c
                    ]
                ),
            }
        summary["profiles"][engine] = {
            "protocols": sorted(
                {r.get("protocol", "legacy-repeated-controls") for r in runs}
            ),
            "physical_query_errors": sum(
                sum(r.get("query_error_types", {}).values()) for r in runs
            ),
            "shared_global_queries": sum(
                r.get("global_exact_control", {}).get("physical_queries", 0)
                for r in runs
            ),
            "completed_lifecycles": len(runs),
            "load_seconds": spread([r["load_seconds"] for r in runs]),
            "build_seconds": spread([r["build_seconds"] for r in runs]),
            "cells": cells,
        }
    protocols = {
        r.get("protocol", "legacy-repeated-controls")
        for runs in records.values()
        for r in runs
    }
    if len(protocols) > 1:
        raise ValueError("mixed protocols cannot be pooled")
    summary["protocols"] = sorted(protocols)
    summary["error_count_scope"] = (
        "per-cell errors count logical scenarios; shared-control physical errors are counted once at profile level"
    )
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    payload = json.dumps(collect(args.root), indent=2, allow_nan=False) + "\n"
    if len(payload.encode()) >= 1048576:
        raise ValueError("summary exceeds retained-file budget")
    with args.out.open("x") as stream:
        stream.write(payload)
