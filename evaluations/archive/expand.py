"""Compare equal-budget hybrid evidence and one shared-fact expansion on the same seeds."""

import argparse
import json
from collections import defaultdict
from pathlib import Path

from evidence import score_pages


def expand(seed, facts, supporting, allowed, budget):
    def eligible(page):
        return page.rsplit("#page=", 1)[0] in allowed

    if any(not eligible(p) for p in seed):
        raise ValueError("hybrid seed already returned an unauthorized page")
    hits = []
    for page in seed:
        hits.append(page)
        # An assertion is usable only through a visible source and visible target evidence.
        neighbors = set()
        for fact in facts.get(page, []):
            neighbors.update(p for p in supporting[tuple(fact)] if eligible(p))
        hits.extend(sorted(neighbors - {page}))
    return list(dict.fromkeys(hits))[:budget]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("workload", "rankings", "assertions", "out"):
        p.add_argument("--" + name, type=Path, required=True)
    p.add_argument("--k", type=int, default=20)
    args = p.parse_args()
    queries = json.loads((args.workload / "queries.json").read_text())
    docs = json.loads((args.workload / "documents.json").read_text())
    gold = json.loads((args.workload / "judgments.json").read_text())
    seeds = json.loads(args.rankings.read_text())
    facts = json.loads(args.assertions.read_text())
    supports = defaultdict(set)
    for page, triples in facts.items():
        for fact in triples:
            supports[tuple(fact)].add(page)
    if not seeds or seeds.keys() - gold.keys():
        raise ValueError("rankings must be a scored tenant phase")
    expanded = {}
    for q in queries:
        key = q["id"]
        if key not in seeds:
            continue
        allowed = {
            doc
            for doc, views in docs.items()
            if q["tenant"] in views
            and views[q["tenant"]].get(q["filter"]["field"]) == q["filter"]["equals"]
        }
        expanded[key] = expand(seeds[key], facts, supports, allowed, args.k)
    selected = {k: gold[k] for k in seeds}
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / "rankings.json").write_text(json.dumps(expanded))
    (args.out / "comparison.json").write_text(
        json.dumps(
            {
                "hybrid": score_pages(selected, seeds, args.k),
                "hybrid_plus_shared_fact": score_pages(selected, expanded, args.k),
                "annotated_pages": len(facts),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
