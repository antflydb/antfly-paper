"""Negative controls for independent stored-vector acceptance."""

import base64
import copy
import json
import unittest

from vector_oracle import (
    VectorOracleError,
    fingerprint,
    negative_controls,
    validate_space,
    verify_snapshot,
)


def fixture():
    hit = {
        "id": "doc",
        "body": "Protected orchard guidance.",
        "tenant": "alpha",
        "version": 5,
        "page": 1,
    }
    source = {key: hit[key] for key in ("tenant", "version", "page")}
    document = json.dumps({key: value for key, value in hit.items() if key != "id"})

    def encode(text):
        return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")

    snapshot = {
        "format": "antfly.dense-index-inspection.v1",
        "index": "b1",
        "index_generation": 7,
        "index_fingerprint": "a" * 64,
        "document_id": "doc",
        "policy_version": "b" * 64,
        "document_json": document,
        "document_fingerprint": fingerprint(document),
        "current": True,
        "vectors": [
            {
                "artifact_id": "af1:embedding:"
                + encode("doc")
                + ":"
                + encode("producer")
                + ":from:chunk:stream:0",
                "producer": "producer",
                "provider": "antfly",
                "model": "model-B",
                "producer_fingerprint": "c" * 64,
                "source_text": hit["body"],
                "source_fingerprint": fingerprint(hit["body"]),
                "values": [1.0] + [0.0] * 383,
            }
        ],
    }
    return snapshot, hit, source


class VectorOracleControls(unittest.TestCase):
    def verify(self, snapshot, hit=None, source=None):
        _, default_hit, default_source = fixture()
        return verify_snapshot(
            snapshot,
            hit or default_hit,
            "b1",
            "model-B",
            "b" * 64,
            source or default_source,
            lambda _: [1.0] + [0.0] * 383,
        )

    def test_real_receipt_negative_control_driver_rejects_every_mutation(self):
        snapshot, _, source = fixture()
        result = negative_controls(snapshot, source, lambda _: [1.0] + [0.0] * 383)
        self.assertEqual(len(result), 7)
        self.assertTrue(all(result.values()))

    def test_exact_independent_vector_matches(self):
        snapshot, _, _ = fixture()
        self.assertEqual(self.verify(snapshot), (7, "a" * 64))

    def test_rejects_wrong_model_stale_missing_nonfinite_or_forged_data(self):
        mutations = {
            "wrong-model": lambda s: s["vectors"][0].update(model="model-A"),
            "wrong-vector": lambda s: s["vectors"][0]["values"].__setitem__(0, 0.0),
            "wrong-dimensions": lambda s: s["vectors"][0].update(values=[1.0]),
            "nonfinite": lambda s: s["vectors"][0]["values"].__setitem__(
                0, float("nan")
            ),
            "bool-vector": lambda s: s["vectors"][0]["values"].__setitem__(0, True),
            "missing-vector": lambda s: s.update(vectors=[]),
            "extra-chunk": lambda s: s["vectors"].append(
                copy.deepcopy(s["vectors"][0])
            ),
            "unpublished": lambda s: s.update(current=False),
            "stale-policy": lambda s: s.update(policy_version="d" * 64),
            "wrong-index": lambda s: s.update(index="a0"),
            "wrong-document": lambda s: s.update(document_id="temporary"),
            "wrong-source-text": lambda s: s["vectors"][0].update(
                source_text="older body"
            ),
            "wrong-source-digest": lambda s: s["vectors"][0].update(
                source_fingerprint="0" * 64
            ),
            "wrong-document-digest": lambda s: s.update(document_fingerprint="0" * 64),
            "wrong-artifact-document": lambda s: s["vectors"][0].update(
                artifact_id="af1:embedding:b3RoZXI:cHJvZHVjZXI"
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                snapshot, _, _ = fixture()
                mutate(snapshot)
                with self.assertRaises(VectorOracleError):
                    self.verify(snapshot)

    def test_rejects_stale_source_even_when_reads_and_inspection_agree(self):
        snapshot, hit, source = fixture()
        source["version"] = 6
        with self.assertRaisesRegex(VectorOracleError, "stale source"):
            self.verify(snapshot, hit, source)

    def test_requires_exact_document_set_absent_deleted_artifacts_and_admin_inspection(
        self,
    ):
        snapshot, hit, source = fixture()

        class App:
            returned = [hit]
            deleted = None
            permits_reader = False
            inspections = 0
            changed = False

            def inspection_witness(self, _):
                return {
                    "policy_version": "b" * 64,
                    "epoch": int(self.changed and self.inspections > 0),
                }

            def query(self, tenant, *_):
                return {
                    "available": True,
                    "hits": self.returned if tenant == "alpha" else [],
                }

            def inspect_vectors(self, identity, _, tenant=None):
                self.inspections += 1
                if tenant is not None and not self.permits_reader:
                    raise OSError("HTTP 403 /inspection: forbidden")
                return self.deleted if identity == "temporary" else snapshot

        def validate(app):
            return validate_space(
                app,
                [[1.0]],
                "b1",
                "model-B",
                {"doc": source},
                lambda _: [1.0] + [0.0] * 383,
                [],
            )

        self.assertTrue(validate(App()))
        for name, setup in {
            "missing": lambda app: setattr(app, "returned", []),
            "duplicate": lambda app: setattr(app, "returned", [hit, hit]),
            "deleted": lambda app: setattr(app, "deleted", snapshot),
            "unauthorized-inspection": lambda app: setattr(app, "permits_reader", True),
            "catalog-changed": lambda app: setattr(app, "changed", True),
        }.items():
            with self.subTest(name=name):
                app = App()
                setup(app)
                with self.assertRaises(VectorOracleError):
                    validate(app)


if __name__ == "__main__":
    unittest.main()
