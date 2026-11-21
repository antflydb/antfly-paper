"""Offline matched-input qualification with independently known pages in incompatible spaces."""

import argparse
import json
from pathlib import Path

from evidence import page_id


def build(out, *, models=None):
    out.mkdir(parents=True, exist_ok=False)
    queries, gold = [], {}
    docs = [
        ("lamp.pdf", "lamps", [1.0, 0.0], [0.0, 1.0]),
        ("rope.pdf", "rope", [0.0, 1.0], [1.0, 0.0]),
    ]
    manifest = {"source": "authored matched vectors", "chunks": 3}
    if models:
        from corpus import EMBEDDER, GEMMA, archive_embed

        words = [row[1] for row in docs]
        a_docs = archive_embed(words, models, EMBEDDER)
        b_docs = archive_embed(words, models, GEMMA)
        docs = [
            (name, word, av, bv)
            for (name, word, _, _), av, bv in zip(docs, a_docs, b_docs, strict=True)
        ]
        a_queries = archive_embed(words, models, EMBEDDER, query=True)
        b_queries = archive_embed(words, models, GEMMA, query=True)
        manifest.update(
            source="authored text with release embeddings",
            embedding_a=EMBEDDER,
            embedding_b=GEMMA,
            dimensions=[len(a_docs[0]), len(b_docs[0])],
        )
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
                    a=docs[0][2],
                    b=docs[0][3],
                    views={"audit": {"review_band": 1, "retention_years": 6}},
                )
            )
            + "\n"
        )
    for i, (name, word, a, b) in enumerate(docs):
        if models:
            a, b = a_queries[i], b_queries[i]
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
        ("manifest", manifest),
        ("queries", queries),
        ("judgments", gold),
    ):
        (out / (file + ".json")).write_text(json.dumps(value, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("out", type=Path)
    p.add_argument(
        "--models",
        help="embed the fixture with BGE A and role-specific EmbeddingGemma 2 B",
    )
    args = p.parse_args()
    build(args.out, models=args.models)
