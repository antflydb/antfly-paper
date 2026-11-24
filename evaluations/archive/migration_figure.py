"""Plot cumulative B writes and A tail latency from privacy-preserving minute bins."""

import argparse
import json
from pathlib import Path


def main():
    import matplotlib.pyplot as plt

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("summary", type=Path)
    p.add_argument("output", type=Path)
    args = p.parse_args()
    trials = json.loads(args.summary.read_text())["trials"]
    fig, (writes, latency) = plt.subplots(
        2, 1, sharex=True, figsize=(7, 4.8), layout="constrained"
    )
    for (key, label), color in zip(
        (("antfly", "Antfly"), ("lance", "LanceDB")),
        ("#235887", "#a24822"),
        strict=True,
    ):
        run = trials[key]
        bins = run["minute_bins"]
        end = run["b_ready_seconds"] / 60
        minutes = [min(row[0] + 1, end) for row in bins]
        writes.plot(
            [0, *minutes],
            [0, *(row[1] / 1000 for row in bins)],
            color=color,
            label=label,
        )
        latency.plot(minutes, [1000 * row[2] for row in bins], color=color, lw=1.15)
    writes.legend(loc="upper left", frameon=False)
    writes.set(ylabel="B rows (thousands)", ylim=(0, None))
    latency.set(
        xlabel="Minutes since B build began", ylabel="A client p95 (ms)", ylim=(0, None)
    )
    for ax in (writes, latency):
        ax.spines[["right", "top"]].set_visible(False)
        ax.grid(axis="y", alpha=0.22)
    fig.savefig(args.output, dpi=180)


if __name__ == "__main__":
    main()
