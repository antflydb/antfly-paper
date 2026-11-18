"""Offline matched-input qualification with independently known pages in incompatible spaces."""

import argparse
import json
from pathlib import Path

from evidence import page_id


def build(out):
    out.mkdir(parents=True, exist_ok=False)
    queries, gold = [], {}
    docs = [
        ("lamp.pdf", "lamps", [1.0, 0.0], [0.0, 1.0]),
        ("rope.pdf", "rope", [0.0, 1.0], [1.0, 0.0]),
    ]
    with (out / "chunks.jsonl").open("w") as stream:
        for name, word, a, b in docs:
            page = page_id(name, 1)
            stream.write(
                json.dumps(
                    dict(
                        id=page,
                        page=page,
                        body=word,
                        a=a,
                        b=b,
                        views={
                            "archive": {"collection_code": "C0", "retention_years": 5},
                            "audit": {"review_band": 1, "retention_years": 6},
                        },
                    )
                )
                + "\n"
            )
        page = page_id("hidden.pdf", 1)
        stream.write(
            json.dumps(
                dict(
                    id=page,
                    page=page,
                    body="lamps",
                    a=[1.0, 0.0],
                    b=[0.0, 1.0],
                    views={"audit": {"review_band": 1, "retention_years": 6}},
                )
            )
            + "\n"
        )
    for name, word, a, b in docs:
        for phase, where in (
            ("initial", {"field": "collection_code", "equals": "C0"}),
            ("after-schema-change", {"field": "retention_years", "equals": 5}),
            ("original", {}),
        ):
            key = word + "-" + phase
            queries.append(
                dict(
                    id=key,
                    question=word,
                    tenant="public" if phase == "original" else "archive",
                    phase=phase,
                    filter=where,
                    a=a,
                    b=b,
                )
            )
            if phase != "original":
                gold[key] = dict(
                    eligible=["lamp.pdf", "rope.pdf"], pages=[page_id(name, 1)]
                )
    for file, value in (
        ("manifest", {"source": "authored matched vectors", "chunks": 3}),
        ("queries", queries),
        ("judgments", gold),
    ):
        (out / (file + ".json")).write_text(json.dumps(value, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("out", type=Path)
    build(p.parse_args().out)
