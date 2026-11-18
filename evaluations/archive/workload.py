"""Compile and score the OfficeQA tenant overlay without exposing answer keys."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import dataset
from evidence import citations, page_id, score_pages

TENANTS = ("archive", "audit")


def compile_workload(files, questions):
    """Assign metadata independently of judgments and preserve evidence coverage."""
    documents = {}
    for name in sorted(files):
        bucket = int(hashlib.sha256(name.encode()).hexdigest(), 16) % 4
        documents[name] = {
            tenant: {
                **({"collection_code": f"C{bucket}"} if tenant == "archive"
                   else {"review_band": bucket}),
                "retention_years": 5 + bucket,
            }
            for tenant in TENANTS if bucket < 2 or tenant == TENANTS[bucket - 2]
        }
    eligible_by_filter = {}
    for name, views in documents.items():
        for tenant, metadata in views.items():
            for field, value in metadata.items():
                eligible_by_filter.setdefault((tenant, field, value), set()).add(name)
    prompts, judgments = [], {}
    for row in questions:
        sources = set(dataset.parse_source_files(row["source_files"]))
        if not sources or sources - documents.keys():
            raise ValueError(f"Missing evidence documents for {row['uid']}")
        for tenant in TENANTS:
            for phase in ("initial", "after-schema-change"):
                for bucket in range(4):
                    field = ("collection_code" if tenant == "archive" else "review_band")
                    value = f"C{bucket}" if tenant == "archive" else bucket
                    if phase == "after-schema-change":
                        field, value = "retention_years", 5 + bucket
                    eligible = eligible_by_filter.get((tenant, field, value), set())
                    relevant = eligible & sources
                    if not relevant:
                        continue
                    key = f"{row['uid']}:{tenant}:{phase}:{bucket}"
                    prompts.append({"id": key, "question": row["question"],
                                    "tenant": tenant, "phase": phase,
                                    "filter": {"field": field, "equals": value}})
                    judgments[key] = {"eligible": sorted(eligible), "relevant": sorted(relevant)}
    return documents, prompts, judgments


def score(judgments, rankings, k):
    if k <= 0 or rankings.keys() != judgments.keys() or not judgments:
        raise ValueError("Expected positive k and exactly one ranking per query")
    recall = complete = ineligible = 0
    for key, expected in judgments.items():
        hits = rankings[key]
        if not isinstance(hits, list) or not all(isinstance(hit, str) for hit in hits):
            raise ValueError(f"Expected a list of document IDs for {key}")
        relevant, eligible = set(expected["relevant"]), set(expected["eligible"])
        top = set(hits[:k])
        found = len(top & relevant)
        recall += found / len(relevant)
        complete += found == len(relevant)
        ineligible += sum(hit not in eligible for hit in hits)
    count = len(judgments)
    return {"queries": count, "k": k, "macro_document_recall": recall / count,
            "all_eligible_evidence_rate": complete / count,
            "ineligible_hit_count": ineligible}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    compile_parser = commands.add_parser("compile")
    compile_parser.add_argument("--data", type=Path, required=True)
    compile_parser.add_argument("--out", type=Path, required=True)
    score_parser = commands.add_parser("score")
    score_parser.add_argument("--judgments", type=Path, required=True)
    score_parser.add_argument("--rankings", type=Path, required=True)
    score_parser.add_argument("--k", type=int, default=20)
    score_parser.add_argument("--pages", action="store_true")
    fetch = commands.add_parser("download")
    fetch.add_argument("--data", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "download":
        dataset.download(args.data, spec=dataset.V2)
        return 0
    if args.command == "score":
        result = (score_pages if args.pages else score)(json.loads(args.judgments.read_text()),
                       json.loads(args.rankings.read_text()), args.k)
        print(json.dumps(result, indent=2))
        return int((result["ineligible"] if args.pages else result["ineligible_hit_count"]) != 0)

    acquisition = dataset.verify_acquisition(args.data, dataset.V2)
    rows = dataset.load_questions(args.data, spec=dataset.V2)
    if len(rows) != dataset.V2.question_count or len({row["uid"] for row in rows}) != len(rows):
        raise ValueError("Expected the complete V2 question set with unique IDs")
    docs, prompts, judgments = compile_workload(
        [path.name for path in dataset.pdf_paths(args.data, spec=dataset.V2)], rows
    )
    counts = {Path(f["path"]).name: f["physical_pages"] for f in acquisition["files"] if f["path"].endswith(".pdf")}
    cited = {r["uid"]: citations(r, counts) for r in rows}
    for row in rows:
        if {doc for doc, _ in cited[row["uid"]]} != set(dataset.parse_source_files(row["source_files"])):
            raise ValueError(f"citation document disagreement for {row['uid']}")
    for key, judgment in judgments.items():
        allowed = set(judgment["eligible"])
        judgment["pages"] = sorted(page_id(doc, n) for doc, n in cited[key.rsplit(":", 3)[0]] if doc in allowed)
    args.out.mkdir(parents=True, exist_ok=False)
    for name, value in (("documents", docs), ("queries", prompts), ("judgments", judgments),
                        ("provenance", {"dataset": dataset.V2.identity(),
                         "acquisition_sha256": dataset.sha256_file(args.data / "dataset-manifest.json"),
                         "raw_pdfs": acquisition["raw_pdfs"], "queries": len(prompts)})):
        (args.out / f"{name}.json").write_text(json.dumps(value, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
