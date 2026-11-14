"""Exercise pinned Embedded APIs, using a local deterministic embedding probe."""

import json
import tempfile
from importlib.metadata import version

import lancedb
from lancedb.embeddings import EmbeddingFunction, get_registry
from lancedb.pydantic import LanceModel, Vector

calls = []


@get_registry().register("paper-probe")
class Probe(EmbeddingFunction):
    def ndims(self):
        return 2

    def compute_source_embeddings(self, texts, *args, **kwargs):
        texts = list(self.sanitize_input(texts))
        calls.extend(texts)
        return [[1.0, 0.0] if text == "before" else [0.0, 1.0] for text in texts]

    def compute_query_embeddings(self, texts, *args, **kwargs):
        return self.compute_source_embeddings(texts, *args, **kwargs)


probe = Probe.create()


class Document(LanceModel):
    id: int
    body: str = probe.SourceField()
    vector: Vector(2) = probe.VectorField()


with tempfile.TemporaryDirectory() as directory:
    table = lancedb.connect(directory).create_table("docs", schema=Document)
    table.add([{"id": 1, "body": "before"}])
    initial = table.to_arrow().to_pylist()[0]
    assert initial["vector"] == [1.0, 0.0]
    table.merge_insert(
        "id"
    ).when_matched_update_all().when_not_matched_insert_all().execute(
        [{"id": 1, "body": "after"}]
    )
    updated = table.to_arrow().to_pylist()[0]
    assert updated["vector"] == [0.0, 1.0]
    before_update = len(calls)
    try:
        table.update(where="id = 1", values={"body": "before"})
        rejected_source_update = False
    except ValueError:
        rejected_source_update = True
    # Installed APIs, rather than assumptions from rolling Enterprise docs.
    stored = table.to_arrow().to_pylist()[0]
    table.delete("id = 1")
    assert table.count_rows() == 0
    print(
        json.dumps(
            {
                "lancedb": version("lancedb"),
                "add_computes_embedding": True,
                "merge_insert_recomputes_embedding": True,
                "source_only_update_rejected": rejected_source_update,
                "source_only_update_calls": len(calls) - before_update,
                "source_only_update_state": stored,
                "delete_removes_current_row": True,
                "embedding_inputs": calls,
                "probe_scope": "deterministic correctness only; no relevance or latency claim",
            },
            indent=2,
        )
    )
