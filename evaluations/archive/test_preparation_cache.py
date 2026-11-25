import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from preparation_cache import DOCUMENT, open_cache

QUERY = "RETRIEVAL_QUERY"


def embed(_endpoint, inputs, _task, identity):
    return [[float(len(str(item)))] * 768 for item in inputs], identity


class PreparationCacheTests(unittest.TestCase):
    def test_edit_resume_duplicates_and_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private.sqlite"
            with (
                patch(
                    "preparation_cache.passage_vectors",
                    side_effect=lambda e, b, i, execute: [
                        (p, None) for p in b.split("|")
                    ],
                ) as plan,
                patch("preparation_cache.embed_batch", side_effect=embed) as model,
            ):
                with open_cache(path, "release1") as cache:
                    first = list(cache.passages("api", "same|old|same", "m1"))
                    self.assertEqual(model.call_args.args[1], ["same", "old"])
                with open_cache(path, "release1") as cache:
                    self.assertEqual(
                        first, list(cache.passages("api", "same|old|same", "m1"))
                    )
                    self.assertEqual(plan.call_count, 1)
                    self.assertEqual(model.call_count, 1)
                    edited = list(cache.passages("api", "same|new|same", "m1"))
                    self.assertEqual(model.call_args.args[1], ["new"])
                    self.assertEqual(edited[0], first[0])
                    cache.embed("api", ["same"], QUERY, "m1")
                    cache.embed("api", ["same"], DOCUMENT, "m2")
                    self.assertEqual(model.call_count, 4)
                with open_cache(path, "release2") as cache:
                    cache.embed("api", ["same"], DOCUMENT, "m1")
                    self.assertEqual(model.call_count, 5)

    def test_image_payload_and_transactional_resume(self):
        a = {
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,YQ=="},
                }
            ]
        }
        b = {
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,Yg=="},
                }
            ]
        }
        with (
            tempfile.TemporaryDirectory() as directory,
            open_cache(Path(directory) / "db", "r") as cache,
        ):
            with patch(
                "preparation_cache.embed_batch", side_effect=RuntimeError("offline")
            ):
                with self.assertRaises(RuntimeError):
                    cache.embed("local", [a], DOCUMENT, "m")
            with patch("preparation_cache.embed_batch", side_effect=embed) as model:
                cache.embed("local", [a], DOCUMENT, "m")
                cache.embed("local", [a, b, a], DOCUMENT, "m")
                self.assertEqual(model.call_args.args[1], [b])
                self.assertEqual(cache.counts[DOCUMENT + "_hits"], 2)
                cache.db.execute("UPDATE preparation_v1 SET checksum=x'00'")
                with self.assertRaisesRegex(ValueError, "checksum"):
                    cache.embed("local", [a], DOCUMENT, "m")

    def test_frozen_queries_keep_original_bytes_and_reject_mismatched_inventory(self):
        prompt = {"id": "q1", "question": "total?", "tenant": "public"}
        frozen = prompt | {"a": [0.25] * 768, "b": [0.25] * 768}
        with (
            tempfile.TemporaryDirectory() as directory,
            open_cache(Path(directory) / "db", "r") as cache,
        ):
            path = Path(directory) / "queries.json"
            raw = json.dumps([frozen], indent=2).encode()
            path.write_bytes(raw)
            cache.seed_queries(path, [prompt], "m")
            self.assertEqual(path.read_bytes(), raw)
            with patch("preparation_cache.embed_batch", side_effect=AssertionError):
                self.assertEqual(
                    cache.embed("api", ["total?"], QUERY, "m")[0], [frozen["a"]]
                )
            with self.assertRaises(ValueError):
                cache.seed_queries(path, [prompt | {"question": "different"}], "m")

    @unittest.skipUnless(
        os.environ.get("OFFICEQA_TEST_MODELS"),
        "requires a local EmbeddingGemma 2 endpoint",
    )
    def test_pinned_model_parity_and_one_word_edit(self):
        from pdf_vector_compare import embed_batch, passage_vectors

        endpoint = os.environ["OFFICEQA_TEST_MODELS"]
        _, identity = embed_batch(endpoint, ["revenue"], QUERY)
        body = " ".join(
            f"Office {i} recorded the eastern warehouse inventory and the cost of three shipments."
            for i in range(64)
        )
        edited = body.replace(
            "Office 12 recorded the eastern", "Office 12 recorded the western"
        )
        fresh = list(passage_vectors(endpoint, body, identity))
        with (
            tempfile.TemporaryDirectory() as directory,
            open_cache(Path(directory) / "db", "qualification") as cache,
        ):
            cold = list(cache.passages(endpoint, body, identity))
            self.assertEqual([p for p, _ in cold], [p for p, _ in fresh])
            for (_, a), (_, b) in zip(cold, fresh, strict=True):
                self.assertLess(
                    sum((x - y) ** 2 for x, y in zip(a, b, strict=True)), 1e-4
                )
            self.assertEqual(cold, list(cache.passages(endpoint, body, identity)))
            prior = cache.counts.copy()
            list(cache.passages(endpoint, edited, identity))
            delta = cache.counts - prior
            self.assertEqual(delta[DOCUMENT + "_computed"], 1)
            self.assertEqual(delta[DOCUMENT + "_hits"], len(cold) - 1)

    def test_historical_passages_are_imported_once_without_model_calls(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            open_cache(Path(directory) / "db", "r") as cache,
        ):
            shard = Path(directory) / "b.jsonl"
            shard.write_text(
                json.dumps({"body": "archived", "b": [0.125] * 768}) + "\n"
            )
            sha = hashlib.sha256(shard.read_bytes()).hexdigest()
            cache.seed_passages(shard, sha, "m")
            cache.seed_passages(shard, sha, "m")
            with patch("preparation_cache.embed_batch", side_effect=AssertionError):
                vectors, _ = cache.embed("api", ["archived"], DOCUMENT, "m")
            self.assertEqual(vectors, [[0.125] * 768])
            self.assertEqual(cache.counts["historical_passage_shards_imported"], 1)
