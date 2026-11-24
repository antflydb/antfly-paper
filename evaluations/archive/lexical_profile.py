"""Profile a frozen question against a dedicated v0.3.1 sorted archive snapshot."""

import argparse
import hashlib
import json
import os
import random
import statistics
import time
from pathlib import Path

from engines import ApiKey, call


def profile(endpoint, table, question, trace, key, rounds):
    selected = question["filter"]
    retention = {
        "field": selected["field"],
        "min": selected["equals"],
        "max": selected["equals"],
        "inclusive_min": True,
        "inclusive_max": True,
    }
    words = question["question"].split()
    unique = list({w.lower(): None for w in words})
    base = dict(
        table=table, fields=["page"], profile=True, limit=100, filter_query=retention
    )

    def match(parts):
        return {"field": "body", "match": " ".join(parts)}

    def query(body):
        result = call(endpoint.rstrip("/") + "/db/v1/query", "POST", body, key)[
            "responses"
        ][0]
        if result["status"] != 200:
            raise ValueError("query failed")
        return [hit["_id"] for hit in result["hits"]["hits"]]

    base["full_text_search"] = match(words)
    original = query(base)
    if len(set(original)) != 100:
        raise ValueError("the baseline requires 100 distinct matches")
    arms = {
        "full_100": base,
        "full_20": base | {"limit": 20},
        "full_1": base | {"limit": 1},
    }
    for label, parts in (
        ("first16", words[:16]),
        ("first50", words[:50]),
        ("second50", words[50:]),
        ("unique", unique),
    ):
        arms[label] = base | {"full_text_search": match(parts)}
    for n in (1, 20, 100):
        arms[f"allow_{n}"] = base | {
            "limit": 1,
            "filter_query": {"conjuncts": [retention, {"ids": original[:n]}]},
        }
    arms["match_all"] = base | {"limit": 1, "full_text_search": {"match_all": {}}}
    cells = {label: [] for label in arms}
    digests = {}
    rng = random.Random(291)
    with trace.open("rb") as log:
        for index in range(-2, rounds):
            order = list(arms)
            rng.shuffle(order)
            for label in order:
                body = arms[label]
                log.seek(0, 2)
                start = time.monotonic()
                ids = query(body)
                ms = (time.monotonic() - start) * 1000
                if len(ids) != body["limit"]:
                    raise ValueError(f"short result in {label}")
                if (
                    label.startswith(("full_", "allow_"))
                    and ids != original[: len(ids)]
                ):
                    raise ValueError(f"rank prefix changed in {label}")
                digest = hashlib.sha256(json.dumps(ids).encode()).digest()
                if digests.setdefault(label, digest) != digest:
                    raise ValueError(f"unstable ranks in {label}")
                lines = [
                    line
                    for line in log.read().decode().splitlines()
                    if "antfly_bench_text_query " in line
                ]
                if len(lines) != 1:
                    raise ValueError(
                        "expected one server ranking trace on an isolated server"
                    )
                d = dict(word.split("=", 1) for word in lines[0].split() if "=" in word)
                if (
                    d["positive_filter"] != "true"
                    or int(d["candidate_iterations"]) != 1
                    or int(d["candidate_window"]) != body["limit"]
                ):
                    raise ValueError(
                        f"unexpected native filter or rank window in {label}"
                    )
                allowed = int(d["filter_doc_nums"])
                if label.startswith("allow_") and allowed != int(label.split("_")[1]):
                    raise ValueError(
                        "ID predicate was not resolved to the expected eligible set"
                    )
                if index >= 0:
                    cells[label].append(
                        dict(
                            allowed=allowed,
                            rows=int(d["snapshot_docs"]),
                            client_ms=ms,
                            engine_ms=int(d["total_us"]) / 1000,
                            execute_ms=int(d["execute_us"]) / 1000,
                        )
                    )
    return {
        "query_id": question["id"],
        "words": len(words),
        "distinct_lowercase_words": len(unique),
        "warmup_rounds_excluded": 2,
        "measured_rounds": rounds,
        "cells": {
            label: {
                **{
                    field: round(statistics.median(r[field] for r in rows), 3)
                    for field in rows[0]
                },
                "same_ordered_ids_as_full_prefix": digests[label]
                == hashlib.sha256(
                    json.dumps(original[: arms[label]["limit"]]).encode()
                ).digest(),
            }
            for label, rows in cells.items()
        },
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for option in ("endpoint", "table", "query-id"):
        p.add_argument("--" + option, required=True)
    for option in ("queries", "trace", "out"):
        p.add_argument("--" + option, type=Path, required=True)
    p.add_argument("--rounds", type=int, default=7)
    args = p.parse_args()
    if args.rounds <= 0:
        p.error("rounds must be positive")
    question = next(
        q for q in json.loads(args.queries.read_text()) if q["id"] == args.query_id
    )
    key = ApiKey(
        os.environ["ARCHIVE_PROFILE_KEY_ID"], os.environ["ARCHIVE_PROFILE_KEY_SECRET"]
    )
    result = profile(args.endpoint, args.table, question, args.trace, key, args.rounds)
    with args.out.open("x") as out:
        out.write(json.dumps(result, indent=2) + "\n")
