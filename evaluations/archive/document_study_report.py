"""Aggregate completed measurements without publishing private page text, queries, or rankings."""

import argparse
import json
from collections import Counter
from pathlib import Path

from native_documents import publish


def summarize(root):
    report = {}
    for cohort in ("sample", "full"):
        cohort_dir = root / cohort
        row = {
            "reader": "LightOnOCR" if cohort == "sample" else "publisher replay",
            "trials": {},
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
            trial = row["trials"][engine] = {"state": "queued"}
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
            if trial["state"] != "complete":
                continue
            attempt = directory / status["attempt"]
            result = json.loads((attempt / "rankings/result.json").read_text())
            score = json.loads((attempt / "original-recall.json").read_text())
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
            trial["public_scores"] = score["scores"]
            trial["public_population"] = score["population"]
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
                traffic = result["traffic"]
                trial["scheduled_arrivals"] = result["scheduled_arrivals"]
                trial["observed_arrivals"] = len(traffic)
                trial["error_arrivals"] = sum(bool(q.get("error")) for q in traffic)
                times = sorted(q["latency"] for q in traffic)
                trial["live_p95_seconds"] = (
                    times[(95 * len(times) - 1) // 100] if times else None
                )
            trial["storage"] = json.loads((attempt / "storage.json").read_text())[
                "allocated_bytes"
            ]
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
