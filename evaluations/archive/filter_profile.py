"""Qualify post-evolution numeric filtering through the archive adapter and tenant reader."""

import argparse
import json
import statistics
import time
import uuid
from pathlib import Path

from engines import Antfly


def qualify(endpoint, *, fresh):
    engine = Antfly(endpoint, "numeric" + uuid.uuid4().hex[:10])
    engine.create(2, evolved=fresh)
    rows = []
    expected = set()
    for i in range(640):
        tenant = "archive" if i % 4 == 0 else "audit"
        row = dict(
            id=f"chunk{i:03}",
            tenant=tenant,
            body="harbor beacon",
            page=f"doc{i:03}.pdf#page=1",
            vec=[1.0, (i + 1) / 640],
        )
        rows.append(row)
    if not fresh:
        for start in range(0, len(rows), 128):
            engine.insert(rows[start : start + 128])
        engine.evolve()
    for i, row in enumerate(rows):
        bucket = (i // 4) % 10
        if bucket != 9:
            row["retention_years"] = 5 + bucket
        if row["tenant"] == "archive" and bucket == 1:
            expected.add(row["id"])
    for start in range(0, len(rows), 128):
        engine.insert(rows[start : start + 128])

    q = {
        "question": "harbor beacon",
        "tenant": "archive",
        "filter": {"field": "retention_years", "equals": 6},
    }
    receipt = {
        "index": engine.text_index,
        "rows": len(rows),
        "eligible_rows": len(expected),
        "channels": {},
    }
    for mode in ("lexical", "dense"):
        timings = []
        for _ in range(3):
            started = time.monotonic()
            result = engine.query(q, [1.0, 0.0], mode, len(expected), profile=True)
            timings.append(time.monotonic() - started)
            ids = [hit["_id"] for hit in result["hits"]["hits"]]
            if len(ids) != len(expected) or set(ids) != expected:
                raise ValueError(
                    f"{mode} differs from the complete tenant and retention oracle"
                )
            profile = result["profile"]
            candidates = (
                profile["sort"]["candidate_count"]
                if mode == "lexical"
                else profile["dense_search"]["native_filter_candidate_count"]
            )
            if candidates != len(expected):
                raise ValueError(
                    f"{mode} filter expected {len(expected)} candidates, observed {candidates}"
                )
        empty = {**q, "filter": {"field": "retention_years", "equals": 99}}
        if engine.search(empty, [1.0, 0.0], mode, len(expected)):
            raise ValueError(f"{mode} returned a row for an absent retention value")
        receipt["channels"][mode] = {
            "candidates": candidates,
            "median_seconds": statistics.median(timings),
        }
    return receipt


def verify_ranking_passes(path, trace):
    """Read the release's native counter after both disposable tables finish."""
    results = json.loads(path.read_text())
    for arm in results.values():
        passes = []
        for line in trace.splitlines():
            if "antfly_bench_text_query " not in line:
                continue
            fields = dict(token.split("=", 1) for token in line.split() if "=" in token)
            if (
                fields["index"] == arm["index"]
                and int(fields["snapshot_docs"]) == arm["rows"]
                and int(fields["hits"]) == arm["eligible_rows"]
            ):
                passes.append(int(fields["candidate_iterations"]))
        if passes != [1, 1, 1]:
            raise ValueError(
                f"expected three single-pass lexical profiles, observed {passes}"
            )
        arm["channels"]["lexical"]["ranking_passes"] = passes
    path.write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--endpoint", required=True, help="disposable Antfly instance")
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args()
    result = {
        "evolved": qualify(args.endpoint, fresh=False),
        "fresh": qualify(args.endpoint, fresh=True),
    }
    args.out.write_text(json.dumps(result, indent=2) + "\n")
