"""Analysis rejects incomplete or altered persisted-vector evidence."""

import base64
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import summarize_application as analysis

SCRIPTS = Path(__file__).resolve().parents[1] / "head-to-head" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from test_vector_oracle import fixture  # noqa: E402


class CountingBoundary(unittest.TestCase):
    def test_inspection_methods_are_separate_from_application_code(self):
        source = "class Adapter:\n    def serve(self):\n        return 1\n    def inspect(self):\n        return 2\n"
        self.assertEqual(analysis.code_lines(source, "Adapter"), 5)
        self.assertEqual(analysis.code_lines(source, "Adapter", ("inspect",)), 3)


class RetainedOracleEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.records = []
        for index in ("a0", "b1", "a2"):
            witness = dict(
                coverage_complete=True,
                shard_observation_complete=True,
                policy_version="b" * 64,
                checkpoint_config_hash=42,
                checkpoint_generation=7,
                index_generation=7,
            )
            model = "model-B" if index == "b1" else "model-A"
            self.records.append(
                dict(
                    kind="validation_attempt",
                    index=index,
                    verification="accepted",
                    before_witness=witness,
                    after_witness=copy.deepcopy(witness),
                )
            )
            for i in range(8):
                snapshot, hit, source = fixture()
                identity = f"d{i:03d}"
                hit["id"] = identity
                snapshot.update(index=index, document_id=identity)
                vector = snapshot["vectors"][0]
                encoded = (
                    base64.urlsafe_b64encode(identity.encode()).decode().rstrip("=")
                )
                vector.update(
                    model=model, artifact_id=f"af1:embedding:{encoded}:cHJvZHVjZXI"
                )
                self.records.append(
                    dict(
                        index=index,
                        document_id=identity,
                        expected_source=source,
                        expected_model=model,
                        expected_witness=witness,
                        serving_hit=hit,
                        snapshot=snapshot,
                        verification="accepted",
                        expected_embedding=[1.0] + [0.0] * 383,
                    )
                )
        self.result = dict(
            checks=[
                dict(
                    name="vector-oracle-negative-controls",
                    controls={
                        name: True
                        for name in (
                            "wrong-model",
                            "wrong-vector",
                            "missing-vector",
                            "unpublished",
                            "dirty-checkpoint",
                            "replay-debt",
                            "wrong-checkpoint-recipe",
                            "stale-policy",
                            "deleted-identity",
                            "stale-source",
                        )
                    },
                )
            ]
        )

    def verify(self):
        payload = json.dumps(self.records).encode()
        (self.directory / "vector-inspections.json").write_bytes(payload)
        (self.directory / "receipt.json").write_text(
            json.dumps(dict(sha256={"vector-inspections.json": analysis.sha(payload)}))
        )

        def committed(revision, path):
            if path.endswith("common.py"):
                return b'EMBEDDER = "model-A"\nMIGRATION_EMBEDDER = "model-B"\n'
            return (SCRIPTS / Path(path).name).read_bytes()

        with patch.object(analysis, "committed", side_effect=committed):
            return analysis.verify_v3_oracle(self.directory, self.result, "fixture")

    def test_complete_evidence(self):
        self.assertEqual(
            self.verify(),
            dict(accepted_vectors=24, stable_spaces=3, max_squared_error=0.0),
        )

    def test_mutations_are_rejected(self):
        original = copy.deepcopy(self.records)
        mutations = {
            "missing-document": lambda: self.records.pop(),
            "duplicate-document": lambda: self.records.append(
                copy.deepcopy(self.records[-1])
            ),
            "wrong-vector": lambda: self.records[1]["snapshot"]["vectors"][0][
                "values"
            ].__setitem__(0, 0.0),
            "stale-checkpoint": lambda: self.records[1]["snapshot"].update(
                checkpoint_generation=6
            ),
            "wrong-model": lambda: self.records[1].update(expected_model="model-B"),
            "unstable-witness": lambda: self.records[0]["after_witness"].update(
                checkpoint_generation=8
            ),
            "incomplete-owner-observation": lambda: self.records[0][
                "before_witness"
            ].update(shard_observation_complete=False),
            "failed-control": lambda: self.result["checks"][0]["controls"].update(
                {"0": False}
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                self.records = copy.deepcopy(original)
                mutate()
                with self.assertRaises((ValueError, AssertionError)):
                    self.verify()


if __name__ == "__main__":
    unittest.main()
