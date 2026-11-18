"""Paired, original-question-clustered evidence recall differences and build interference."""

import argparse
import json
import math
import random
from collections import defaultdict
from pathlib import Path
from statistics import mean


def paired(left, right, seed=1701):
    if left.keys() != right.keys() or not left:
        raise ValueError("quality cells must cover identical nonempty query IDs")
    groups = defaultdict(list)
    for key in sorted(left):
        groups[key.split(":", 1)[0]].append(left[key]["recall"] - right[key]["recall"])
    deltas = [mean(g) for g in groups.values()]
    rng = random.Random(seed)
    samples = sorted(mean(rng.choices(deltas, k=len(deltas))) for _ in range(2000))
    return {
        "original_questions": len(groups),
        "mean_within_question_recall_difference": mean(deltas),
        "paired_bootstrap_95": [samples[49], samples[1949]],
        "seed": seed,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run", type=Path)
    p.add_argument("--against", type=Path)
    p.add_argument("--figure", type=Path)
    args = p.parse_args()
    d = json.loads(args.run.read_text())
    result = {}
    if args.against:
        peer = json.loads(args.against.read_text())
        if (d["source"], d["k"], d["depth"]) != (
            peer["source"],
            peer["k"],
            peer["depth"],
        ):
            raise ValueError("mismatched workload or scoring depth")
        for key, a in d["results"].items():
            if "quality" in a:
                result[key] = paired(
                    a["quality"]["per_query"],
                    peer["results"][key]["quality"]["per_query"],
                )
    traffic = d["traffic"]
    result["observed"] = len(traffic)
    result["errors"] = sum("error" in t for t in traffic)
    result["missing_arrivals"] = max(0, d["scheduled_arrivals"] - len(traffic))
    print(json.dumps(result, indent=2))
    if args.figure:
        import matplotlib.pyplot as plt

        fig, (ax, lat) = plt.subplots(
            2, 1, sharex=True, figsize=(6, 4), layout="constrained"
        )
        points = [[0, 0], *d["acknowledged_rows_over_time"]]
        ax.step([x for x, _ in points], [n for _, n in points], where="post")
        ax.set_ylabel("B rows acknowledged")
        latency = defaultdict(list)
        for r in traffic:
            latency[int(r["arrival"] // 5)].append(r["latency"] * 1000)
            if "error" in r:
                lat.plot(r["arrival"], r["latency"] * 1000, "rx")
        bins = sorted(latency)
        lat.plot(
            [5 * b for b in bins],
            [sorted(latency[b])[math.ceil(0.95 * len(latency[b])) - 1] for b in bins],
            "o-",
        )
        lat.set(xlabel="Seconds since B build started", ylabel="A query p95 (ms)")
        fig.savefig(args.figure)


if __name__ == "__main__":
    main()
