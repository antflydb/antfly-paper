"""Publishable aggregate measurements for the matched Linux trials and supplemental PostgreSQL run."""

import argparse
import hashlib
import json
from pathlib import Path

from campaign_report import traffic
from compare_receipts import timings
from corpus_controls import ACCESS


def measurements(root):
    data = {}
    paths = {"PostgreSQL": root / "postgres"}
    for n in (1, 2):
        for mode in ACCESS:
            paths[f"{n} {mode}"] = root / "frozen-b" / f"round-{n}-{mode}"
    reference = None
    for label, folder in paths.items():
        receipt = folder / "result.json"
        if not receipt.is_file():
            continue
        raw = receipt.read_bytes()
        r = json.loads(raw)
        engine = "postgres" if label == "PostgreSQL" else label.split()[1].split("_")[0]
        signature = tuple(
            r[k] for k in ("source", "rows_per_generation", "k", "depth", "qps")
        )
        if r["engine"] != engine or (reference is not None and signature != reference):
            raise ValueError("workload or engine differs")
        reference = signature
        counts = r["b_indexed_counts"]
        if (
            r["readiness_probe_ordinal"] != 284
            or not counts
            or any(v != r["rows_per_generation"] for v in counts.values())
        ):
            raise ValueError("probe or coverage differs")
        active = traffic(r["traffic"])
        channels = timings(r["traffic"])["p95_ms"]
        m = r["model_metrics"]
        row = {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "rows": r["rows_per_generation"],
            "ready_s": r["b_ready_seconds"],
            "write_calls_s": m["write_call_seconds"],
            "model_calls_s": m["model_seconds"],
            "query_p95_ms": active["p95_ms"],
            "lexical_ms": channels["lexical"],
            "dense_ms": channels["dense"],
            "missing": max(0, r["scheduled_arrivals"] - active["arrivals"]),
            "errors": active["errors"],
            "ineligible": active["ineligible"],
            "recall": r["results"]["a-after-schema-change-hybrid"]["quality"]["recall"],
        }
        if label == "PostgreSQL":
            if m["embedded_chunks"] != r["source"]["chunks"]:
                raise ValueError("incomplete B inference")
            row["any_recall"] = r["postgres_lexical_sensitivity"][
                "a-after-schema-change-hybrid-any-lexeme"
            ]["quality"]["recall"]
            ranks = json.loads(
                (folder / "a-after-schema-change-lexical-any-lexeme.json").read_text()
            )
            row["any_lexical_empty"] = sum(not pages for pages in ranks.values())
        elif m["embedded_chunks"] or r["access"] != ACCESS[label.split()[1]]:
            raise ValueError("unexpected embedding work or access configuration")
        data[label] = row
    return data


def markdown(data):
    lines = [
        "# Matched OfficeQA follow-ups",
        "",
        "The PostgreSQL trial includes fresh B inference and keeps the original full-question lexical parser. Its supplemental disjunctive replay runs after timing. The Linux controls use frozen B vectors, the same 4-CPU/4-GiB server limits, and two opposite trial orders. Each arm uses evolved workload probe 284. The [comparison audit](../COMPARABILITY.md) records how that probe was selected.",
        "",
    ]
    if not data:
        return "\n".join([*lines, "Awaiting completed follow-up receipts.", ""])
    lines += [
        "| Trial | Rows | B ready (s) | B write calls (s) | B model calls (s) | A scheduled / lexical / dense p95 (ms) | Missing / errors / ineligible | A hybrid recall@20 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, row in data.items():
        lines.append(
            f"| {label} | {row['rows']:,} | {row['ready_s']:.2f} | {row['write_calls_s']:.2f} | {row['model_calls_s']:.2f} | {row['query_p95_ms']} / {row['lexical_ms']} / {row['dense_ms']} | {row['missing']} / {row['errors']} / {row['ineligible']} | {row['recall']:.2%} |"
        )
    if pg := data.get("PostgreSQL"):
        lines += [
            "",
            f"PostgreSQL any-lexeme A hybrid recall@20 was {pg['any_recall']:.2%}. Its lexical channel returned no pages for {pg['any_lexical_empty']} evolved variants.",
        ]
    lines += ["", "Full receipt SHA-256 digests", ""]
    lines.extend(f"- {label} `{r['sha256']}`" for label, r in data.items())
    planned = [f"{n} {mode}" for n in (1, 2) for mode in ACCESS]
    missing = [label for label in planned if label not in data]
    if missing:
        lines.extend(
            [
                "",
                "Frozen-vector trials without complete receipts: "
                + ", ".join(missing)
                + ".",
            ]
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("directory", type=Path)
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    d = measurements(args.directory)
    print(json.dumps(d, allow_nan=False) if args.json else markdown(d), end="")
