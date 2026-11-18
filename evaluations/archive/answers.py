"""Answer original V2 questions from retrieved or oracle pages with one fixed reader budget."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import dataset
from corpus import chunk_rows, page_id
from evidence import citations


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for arg in ("data", "corpus", "out"):
        p.add_argument("--" + arg, type=Path, required=True)
    arm = p.add_mutually_exclusive_group(required=True)
    arm.add_argument("--rankings", type=Path)
    arm.add_argument("--oracle-pages", action="store_true")
    p.add_argument("--model", required=True)
    p.add_argument("--endpoint", required=True, help="chat completions URL")
    p.add_argument("--context-chars", type=int, default=48000)
    p.add_argument("--max-tokens", type=int, default=512)
    args = p.parse_args()
    if min(args.context_chars, args.max_tokens) < 1:
        p.error("budgets must be positive")
    manifest = dataset.verify_acquisition(args.data)
    questions = dataset.load_questions(args.data, spec=dataset.V2)
    counts = {
        Path(f["path"]).name: f["physical_pages"]
        for f in manifest["files"]
        if f["path"].endswith(".pdf")
    }
    gold = {
        q["uid"]: {page_id(doc, n) for doc, n in citations(q, counts)}
        for q in questions
    }
    ranks = (
        json.loads(args.rankings.read_text())
        if args.rankings
        else {k: sorted(v) for k, v in gold.items()}
    )
    if ranks.keys() != gold.keys():
        raise ValueError("answer arm requires exactly the original 90 queries")
    wanted = {page for hits in ranks.values() for page in hits}
    pages = {}
    for chunk in chunk_rows(args.corpus):
        if chunk["page"] in wanted:
            pages.setdefault(chunk["page"], []).append(chunk["body"])
    spec = importlib.util.spec_from_file_location(
        "officeqa_reward", args.data / "reward.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args.out.mkdir(parents=True, exist_ok=False)
    outcomes = []
    with (args.out / "answers.jsonl").open("w") as out:
        for row in questions:
            uid = row["uid"]
            evidence = ""
            supplied = []
            for key in ranks[uid]:
                text = " ".join(pages.get(key, []))
                if text and len(evidence) + len(key) + 4 < args.context_chars:
                    evidence += (f"[{key}] " + text + "\n")[
                        : args.context_chars - len(evidence)
                    ]
                    supplied.append(key)
            prompt = (
                "Answer using only these source pages. Return a JSON object with answer (string) "
                "and citations (array of exact page IDs).\nQuestion: "
                + row["question"]
                + "\n"
                + evidence
            )
            from urllib.request import Request, urlopen

            body = {
                "model": args.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": args.max_tokens,
            }
            headers = {"Content-Type": "application/json"}
            if os.getenv("ARCHIVE_READER_TOKEN"):
                headers["Authorization"] = (
                    "Bearer " + os.environ["ARCHIVE_READER_TOKEN"]
                )
            with urlopen(
                Request(args.endpoint, data=json.dumps(body).encode(), headers=headers),
                timeout=180,
            ) as r:
                reply = json.load(r)
            raw = reply["choices"][0]["message"]["content"]
            try:
                parsed = json.loads(raw)
                answer = parsed["answer"]
                cited = parsed["citations"]
                if (
                    not isinstance(answer, str)
                    or not isinstance(cited, list)
                    or not all(isinstance(c, str) for c in cited)
                ):
                    raise ValueError("malformed answer")
            except (ValueError, TypeError, KeyError):
                answer, cited = "", []
            supported = set(cited) & gold[uid] & set(supplied)
            record = {
                "uid": uid,
                "answer": answer,
                "raw": raw,
                "citations": cited,
                "supplied": supplied,
                "exact": module.score_answer(row["answer"], answer, 0.0)
                if answer
                else 0,
                "correct_citations": len(supported),
                "returned_citations": len(set(cited)),
                "required_citations": len(gold[uid]),
                "usage": reply.get("usage"),
                "context_sha256": hashlib.sha256(evidence.encode()).hexdigest(),
            }
            out.write(json.dumps(record) + "\n")
            out.flush()
            outcomes.append(record)
    result = {
        "questions": len(outcomes),
        "exact": sum(r["exact"] for r in outcomes) / len(outcomes),
        "model": args.model,
        "source": json.loads((args.corpus / "manifest.json").read_text()),
        "arm": "oracle" if args.oracle_pages else "retrieved",
        "context_chars": args.context_chars,
        "max_tokens": args.max_tokens,
    }
    (args.out / "result.json").write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
