"""Measure retrieval of publisher-cited physical pages for the original OfficeQA questions."""

import argparse
import hashlib
import json
from pathlib import Path

import dataset
from evidence import citations, page_id, score_pages


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", required=True, type=Path)
    p.add_argument(
        "--result",
        required=True,
        type=Path,
        help="directory with result.json and rankings",
    )
    args = p.parse_args()
    source = args.result / "result.json"
    receipt = json.loads(source.read_text())
    manifest = (args.data / "dataset-manifest.json").read_bytes()
    if hashlib.sha256(manifest).hexdigest() != receipt["source"]["acquisition_sha256"]:
        raise ValueError("rankings come from a different dataset")
    files = json.loads(manifest)["files"]
    expected = next(
        f["local_sha256"] for f in files if f["path"] == dataset.V2.csv_name
    )
    if dataset.sha256_file(args.data / dataset.V2.csv_name) != expected:
        raise ValueError("question or citation inventory changed")
    counts = {
        Path(f["path"]).name: f["physical_pages"]
        for f in files
        if f["path"].endswith(".pdf")
    }
    gold = {
        row["uid"]: {
            "eligible": counts.keys(),
            "pages": [page_id(name, n) for name, n in citations(row, counts)],
        }
        for row in dataset.load_questions(args.data, spec=dataset.V2)
    }
    scores = {}
    for channel in ("lexical", "dense", "hybrid"):
        ranking = json.loads((args.result / f"a-original-{channel}.json").read_text())
        scored = score_pages(gold, ranking, receipt["k"])
        scores[channel] = {
            k: scored[k] for k in ("k", "queries", "recall", "complete", "ineligible")
        }
    print(
        json.dumps(
            {
                "engine": receipt["engine"],
                "source": receipt["source"]["source"],
                "receipt_sha256": dataset.sha256_file(source),
                "scores": scores,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
