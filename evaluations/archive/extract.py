"""Extract supported relations on a frozen physical-page sample with a common reader."""

import argparse
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

from annotate import assertions
from corpus import chunk_rows


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("sample", "corpus", "out"):
        p.add_argument("--" + key, type=Path, required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--endpoint", required=True)
    p.add_argument("--max-tokens", type=int, default=1024)
    p.add_argument("--context-chars", type=int, default=24000)
    args = p.parse_args()
    if min(args.context_chars, args.max_tokens) < 1:
        p.error("positive budgets required")
    pages = json.loads(args.sample.read_text())["pages"]
    content = {page: [] for page in pages}
    for row in chunk_rows(args.corpus):
        if row["page"] in content:
            content[row["page"]].append(row["body"])
    args.out.mkdir(parents=True, exist_ok=False)
    observed = {}
    with (args.out / "trace.jsonl").open("w") as trace:
        for page, parts in content.items():
            body = " ".join(parts)[: args.context_chars]
            triples = []
            record = {"page": page, "characters": len(body)}
            if body:
                prompt = (
                    "Read one historical Treasury page. Extract only facts whose agency, "
                    "expenditure item and fiscal year this page identifies explicitly. "
                    "Return a JSON list of triples [agency, paid-for, item] and "
                    "[item, fiscal-year, year]. Copy names faithfully in lowercase and use four-digit "
                    "years. Omit guessed relationships. Return [] for no supported facts.\n"
                    + body
                )
                request = {
                    "model": args.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": args.max_tokens,
                    "temperature": 0,
                }
                headers = {"Content-Type": "application/json"}
                if token := os.getenv("ARCHIVE_READER_TOKEN"):
                    headers["Authorization"] = "Bearer " + token
                with urlopen(
                    Request(
                        args.endpoint,
                        data=json.dumps(request).encode(),
                        headers=headers,
                    ),
                    timeout=180,
                ) as r:
                    data = json.load(r)
                raw = data["choices"][0]["message"]["content"]
                record.update(raw=raw, usage=data.get("usage"))
                try:
                    triples = json.loads(raw)
                    if not isinstance(triples, list):
                        raise ValueError("expected list of triples")
                    assertions({page: triples}, [page])
                    if any(
                        row[1] not in ("paid-for", "fiscal-year") for row in triples
                    ):
                        raise ValueError("unrecognized relation")
                except (ValueError, TypeError):
                    triples = []
                    record["invalid_output"] = True
            observed[page] = triples
            trace.write(json.dumps(record) + "\n")
            trace.flush()
    (args.out / "assertions.json").write_text(json.dumps(observed, indent=2))
    (args.out / "settings.json").write_text(
        json.dumps(
            dict(
                model=args.model,
                context_chars=args.context_chars,
                max_tokens=args.max_tokens,
                sample=json.loads(args.sample.read_text()),
                source=json.loads((args.corpus / "manifest.json").read_text()),
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
