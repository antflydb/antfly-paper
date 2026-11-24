"""Freeze a blind, reproducible page sample and score externally annotated supported assertions."""

import argparse
import hashlib
import json
from pathlib import Path

from evidence import page_id, score_assertions


def sample(inventory, count, seed):
    # Hash-ranking all page IDs permits replication independent of file order.
    pool = []
    for row in inventory["files"]:
        if row["path"].endswith(".pdf"):
            for n in range(1, row["physical_pages"] + 1):
                page = page_id(Path(row["path"]).name, n)
                pool.append(
                    (hashlib.sha256((seed + "\0" + page).encode()).digest(), page)
                )
    if not 1 <= count <= len(pool):
        raise ValueError("sample size outside physical corpus")
    return [page for _, page in sorted(pool)[:count]]


def assertions(rows, expected):
    if set(rows) != set(expected):
        raise ValueError(
            "each frozen page needs a list, including pages with zero assertions"
        )
    found = []
    for key, items in rows.items():
        for row in items:
            if len(row) != 3 or not all(isinstance(x, str) and x.strip() for x in row):
                raise ValueError(f"invalid assertion for {key}")
            found.append([*row, key])
    return found


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    freeze = sub.add_parser("freeze")
    freeze.add_argument("inventory", type=Path)
    freeze.add_argument("--pages", type=int, default=100)
    freeze.add_argument("--seed", default="archive-graph-v1")
    score = sub.add_parser("score")
    for arg in ("sample", "gold", "observed"):
        score.add_argument(arg, type=Path)
    args = p.parse_args()
    if args.cmd == "freeze":
        chosen = sample(json.loads(args.inventory.read_text()), args.pages, args.seed)
        result = {
            "seed": args.seed,
            "pages": chosen,
            "inventory_sha256": hashlib.sha256(args.inventory.read_bytes()).hexdigest(),
        }
    else:
        chosen = json.loads(args.sample.read_text())["pages"]
        result = score_assertions(
            assertions(json.loads(args.gold.read_text()), chosen),
            assertions(json.loads(args.observed.read_text()), chosen),
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
