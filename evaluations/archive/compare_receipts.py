"""Audit paired input coverage, query timing boundaries and probe coverage on private receipts."""

import argparse
import hashlib
import json
import math
from pathlib import Path

from evidence import score_pages
from report import paired


def read(path):
    return json.loads(path.read_text())


def percentile_ms(values):
    return (
        round(sorted(values)[math.ceil(0.95 * len(values)) - 1] * 1000, 2)
        if values
        else None
    )


def timings(rows):
    ok = [r for r in rows if not r.get("error")]
    return {
        "arrivals": len(rows),
        "failures": len(rows) - len(ok),
        "p95_ms": {
            "scheduled_to_done": percentile_ms([r["latency"] for r in ok]),
            "dispatched_to_done": percentile_ms(
                [r["completed"] - r["dispatched"] for r in ok]
            ),
            "dispatch_delay": percentile_ms(
                [r["dispatched"] - r["arrival"] for r in ok]
            ),
            **{
                mode: percentile_ms([r["channel_seconds"][mode] for r in ok])
                for mode in ("lexical", "dense")
            },
        },
    }


def audit(workload, paths, k):
    gold = read(workload / "judgments.json")
    ids = [
        q["id"]
        for q in read(workload / "queries.json")
        if q["phase"] == "after-schema-change"
    ]
    selected = {i: gold[i] for i in ids}
    eligible = set(ids)
    result = {
        "variants": len(ids),
        "judgments_sha256": hashlib.sha256(
            (workload / "judgments.json").read_bytes()
        ).hexdigest(),
        "subjects": {},
    }
    scores = {}
    reference = None
    for engine, folder in paths:
        ranks = {
            c: read(folder / f"a-after-schema-change-{c}.json")
            for c in ("lexical", "dense", "hybrid")
        }
        if any(row.keys() != selected.keys() for row in ranks.values()):
            raise ValueError("different query set")
        coverage = {i for i in ids if ranks["lexical"][i] and ranks["dense"][i]}
        eligible &= coverage
        measured = score_pages(selected, ranks["hybrid"], k)
        scores[engine] = measured.pop("per_query")
        row = {
            "hybrid": measured,
            "empty": {c: sum(not v for v in r.values()) for c, r in ranks.items()},
            "first_probe_page_counts": {
                c: len(ranks[c][ids[0]]) for c in ("lexical", "dense")
            },
            "both_nonempty": len(coverage),
        }
        p = folder / "result.json"
        if p.exists():
            r = read(p)
            signature = (r["source"], r["k"], r["depth"], r["qps"], r["migration"])
            if (
                r["engine"] != engine
                or k != r["k"]
                or (reference is not None and signature != reference)
            ):
                raise ValueError("completed run configuration differs")
            reference = signature
            row["receipt_sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
            model = r["model_metrics"]["model_seconds"]
            ready = r["b_ready_seconds"]
            row["build_seconds"] = {
                "total": round(ready, 3),
                "model_calls": round(model, 3),
                "outside_model_calls": round(ready - model, 3),
                "after_last_write": round(ready - r["b_writes_complete_seconds"], 3),
            }
            row["traffic"] = {s: timings(r[s]) for s in ("baseline_traffic", "traffic")}
        result["subjects"][engine] = row
    result["common_two_channel_queries"] = len(eligible)
    result["first_common_ordinal_1_based"] = next(
        (n for n, i in enumerate(ids, 1) if i in eligible), None
    )
    if "antfly" in scores:
        result["paired_hybrid_recall_antfly_minus_peer"] = {
            peer: paired(scores["antfly"], values)
            for peer, values in scores.items()
            if peer != "antfly"
        }
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workload", required=True, type=Path)
    p.add_argument("--k", type=int, default=20)
    p.add_argument(
        "--run", action="append", nargs=2, required=True, metavar=("ENGINE", "DIR")
    )
    args = p.parse_args()
    print(
        json.dumps(
            audit(args.workload, [(e, Path(d)) for e, d in args.run], args.k), indent=2
        )
    )
