"""Aggregate completed measurements without publishing private page text, queries, or rankings."""

import argparse
import json
from collections import Counter
from pathlib import Path

from native_documents import publish


def traffic_summary(rows, expected):
    ok = [r for r in rows if "latency" in r and not r.get("error")]
    times = sorted(r["latency"] for r in ok)
    scored = [r for r in ok if "cited_page_returned" in r]
    return {
        "scheduled": expected,
        "observed": len(rows),
        "errors": sum(bool(r.get("error")) for r in rows),
        "missed": max(0, expected - len(rows)),
        "capacity_drops": sum(
            r.get("error") == "load generator at concurrency limit" for r in rows
        ),
        "completed": len(ok),
        "empty": sum(r.get("hit_count", len(r.get("hits", []))) == 0 for r in ok),
        "ineligible_pages": sum(r.get("ineligible", 0) for r in ok),
        "citation_scored": len(scored),
        "returned_citation": sum(r["cited_page_returned"] for r in scored),
        "p95_seconds": times[(95 * len(times) - 1) // 100] if times else None,
    }


def summarize(root):
    report = {}
    for cohort in ("sample", "full"):
        cohort_dir = root / cohort
        row = {
            "reader": "LightOnOCR" if cohort == "sample" else "publisher replay",
            "trials": {},
        }
        skipped = cohort_dir / "skipped.json"
        if skipped.is_file():
            detail = json.loads(skipped.read_text())
            row["stopped"] = {
                k: detail[k] for k in ("reason", "committed_pdfs", "committed_pages")
            }
        for engine in (
            "antfly",
            "postgres",
            "opensearch",
            "vespa",
            "lance",
            "antfly-native",
        ):
            directory = cohort_dir / (
                "native-results" if engine == "antfly-native" else "matched-results"
            )
            marker = directory / (engine + ".json")
            trial = row["trials"][engine] = {
                "state": "skipped" if "stopped" in row else "queued"
            }
            if not marker.exists():
                continue
            status = json.loads(marker.read_text())
            trial["state"] = (
                "running"
                if "end" not in status
                else "complete"
                if status["success"]
                else "failed"
            )
            if trial["state"] == "running":
                continue
            attempt = directory / status["attempt"]
            result_file = attempt / "rankings/result.json"
            if trial["state"] == "failed" and not result_file.exists():
                continue
            result = json.loads(result_file.read_text())
            score_file = attempt / "original-recall.json"
            if score_file.exists() or trial["state"] == "complete":
                score = json.loads(score_file.read_text())
                trial["public_scores"] = score["scores"]
                trial["public_population"] = score["population"]
            trial.update(
                {
                    k: result[k]
                    for k in [
                        "a_build_seconds",
                        "a_ready_seconds",
                        "b_ready_seconds",
                        "cutover_seconds",
                        "live_p95_seconds",
                        "extraction",
                        "replay_requests",
                        "application_stage_counts",
                    ]
                    if k in result
                }
            )
            trial["tenant_scores"] = {
                name: {
                    k: v
                    for k, v in m["quality"].items()
                    if k in ("queries", "recall", "complete", "ineligible")
                }
                for name, m in result["results"].items()
                if "after-schema-change" in name and "quality" in m
            }
            if "live_closed_loop" in result:
                trial["live_closed_loop"] = result["live_closed_loop"]
                counts = Counter()
                with (attempt / "rankings/live.jsonl").open() as f:
                    for line in f:
                        q = json.loads(line)
                        if "cited_page_returned" in q:
                            counts["probes"] += 1
                            counts["returned"] += q["cited_page_returned"]
                trial["live_citation_probes"] = dict(counts)
            else:
                for key, rows, expected in (
                    ("migration", result["traffic"], result["scheduled_arrivals"]),
                    (
                        "baseline",
                        result["baseline_traffic"],
                        len(result["baseline_traffic"]),
                    ),
                ):
                    trial[key] = traffic_summary(rows, expected)
                trial["traffic_protocol"] = result.get(
                    "traffic_protocol", "scheduled-a-reads-v1"
                )
                trial["scheduled_arrivals"] = result["scheduled_arrivals"]
                trial["observed_arrivals"] = len(result["traffic"])
                trial["live_p95_seconds"] = trial["migration"]["p95_seconds"]
                cutover = result.get("cutover_probe", {})
                trial["cutover_b_returned_pages"] = cutover.get(
                    "encoder"
                ) == "b" and bool(cutover.get("pages"))
            storage = attempt / "storage.json"
            if storage.exists():
                trial["storage"] = json.loads(storage.read_text())["allocated_bytes"]
            if "a_status" in result:
                trial["searchable_rows"] = {
                    gen: result[gen + "_status"]["vectors"] for gen in ("a", "b")
                }
            elif "a_rows" in result:
                trial["searchable_rows"] = {
                    gen: result[gen + "_rows"] for gen in ("a", "b")
                }
        estimate = cohort_dir / "cost-estimate.json"
        if estimate.is_file():
            cost = json.loads(estimate.read_text())
            row["shared_worker_estimate"] = {
                k: v for k, v in cost.items() if k != "sample"
            }
            row["measured_sample"] = [
                {k: v for k, v in r.items() if k != "sha256"} for r in cost["sample"]
            ]
        report[cohort] = row
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    publish(args.out, summarize(args.root))
