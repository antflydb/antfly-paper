"""Contract controls for the real per-shard index-status response shape."""

import copy
import unittest

from adapters import Antfly


def fixture():
    # Aggregate status intentionally has no physical checkpoint fields. This
    # reproduces the response that stopped the frozen real-model qualification.
    return {
        "config": {"type": "embeddings", "dimension": 384, "field": "body"},
        "status": {
            "runtime_present": True,
            "runtime_fresh": True,
            "expected_groups": 1,
            "reported_groups": 1,
            "fresh_groups": 1,
            "stale_groups": 0,
            "missing_groups": 0,
            "unknown_remote_groups": 0,
            "coverage": {"complete": True, "observation_complete": True},
            "incarnation": "g-0000000000000007",
        },
        "shard_status": {
            "8158423500563204517": {
                "runtime_present": True,
                "runtime_fresh": True,
                "coverage": {"complete": True, "observation_complete": True},
                "incarnation": "g-0000000000000007",
                "projection_checkpoint_status": "clean",
                "projection_checkpoint_config_fingerprint": "000000000000002a",
                "projection_checkpoint_generation": 7,
            },
        },
    }


def witness(index):
    app = object.__new__(Antfly)
    calls = []

    def api(path):
        calls.append(path)
        return (
            {"policy_version": "b" * 64, "etag": "privacy-1"}
            if path.endswith("/privacy")
            else index
        )

    app.api = api
    result = app.inspection_witness("b/1")
    return result, calls


class CheckpointWitnessContract(unittest.TestCase):
    def test_real_response_shape_uses_only_the_observed_shard_checkpoint(self):
        index = fixture()
        self.assertNotIn("projection_checkpoint_config_fingerprint", index["status"])
        result, calls = witness(index)
        self.assertTrue(result["shard_observation_complete"])
        self.assertEqual(result["checkpoint_shard"], "8158423500563204517")
        self.assertEqual(result["checkpoint_config_hash"], 42)
        self.assertEqual(result["checkpoint_generation"], 7)
        self.assertEqual(result["index_generation"], 7)
        self.assertEqual(
            calls,
            [
                "/db/v1/tables/documents/indexes/b%2F1",
                "/db/v1/tables/documents/privacy",
            ],
        )
        # An aggregate identity is neither required nor substituted for the
        # physical owner, even if a future response exposes different values.
        index["status"]["projection_checkpoint_config_fingerprint"] = "f" * 16
        index["status"]["projection_checkpoint_generation"] = 999
        changed, _ = witness(index)
        self.assertEqual(changed, result)

    def test_missing_or_multiple_owners_cannot_mint_a_checkpoint_witness(self):
        for count in (0, 2):
            index = fixture()
            shard = next(iter(index["shard_status"].values()))
            index["shard_status"] = {str(i): copy.deepcopy(shard) for i in range(count)}
            result, _ = witness(index)
            self.assertFalse(result["shard_observation_complete"])
            self.assertIsNone(result["checkpoint_shard"])
            self.assertIsNone(result["checkpoint_config_hash"])
            self.assertEqual(len(result["shards"]), count)

    def test_incomplete_stale_unknown_or_malformed_observations_fail_closed(self):
        mutations = {
            "extra-expected-owner": lambda a, s: a.update(expected_groups=2),
            "missing-owner": lambda a, s: a.update(missing_groups=1),
            "unknown-owner": lambda a, s: a.update(unknown_remote_groups=1),
            "stale-owner": lambda a, s: a.update(stale_groups=1, fresh_groups=0),
            "bool-count": lambda a, s: a.update(expected_groups=True),
            "aggregate-stale": lambda a, s: a.update(runtime_fresh=False),
            "aggregate-incomplete": lambda a, s: a["coverage"].update(
                observation_complete=False
            ),
            "owner-absent": lambda a, s: s.update(runtime_present=False),
            "owner-stale": lambda a, s: s.update(runtime_fresh=False),
            "owner-incomplete": lambda a, s: s["coverage"].update(complete=False),
            "observation-incomplete": lambda a, s: s["coverage"].update(
                observation_complete=False
            ),
            "wrong-incarnation": lambda a, s: s.update(
                incarnation="g-0000000000000008"
            ),
            "unbound-incarnation": lambda a, s: a.update(
                incarnation="g-0000000000000000"
            ),
            "dirty-checkpoint": lambda a, s: s.update(
                projection_checkpoint_status="rebuilding"
            ),
            "missing-hash": lambda a, s: s.pop(
                "projection_checkpoint_config_fingerprint"
            ),
            "malformed-hash": lambda a, s: s.update(
                projection_checkpoint_config_fingerprint="invalid"
            ),
            "missing-generation": lambda a, s: s.pop(
                "projection_checkpoint_generation"
            ),
            "bool-generation": lambda a, s: s.update(
                projection_checkpoint_generation=True
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                index = fixture()
                shard = next(iter(index["shard_status"].values()))
                mutate(index["status"], shard)
                result, _ = witness(index)
                self.assertFalse(result["shard_observation_complete"])
                self.assertEqual(len(result["shards"]), 1)

    def test_owner_change_and_incomplete_end_remain_observable(self):
        index = fixture()
        before, _ = witness(index)
        shard = next(iter(index["shard_status"].values()))
        index["shard_status"] = {"new-owner": shard}
        moved, _ = witness(index)
        self.assertTrue(moved["shard_observation_complete"])
        self.assertEqual(
            moved["checkpoint_config_hash"], before["checkpoint_config_hash"]
        )
        self.assertNotEqual(moved, before)
        shard["coverage"]["complete"] = False
        incomplete, _ = witness(index)
        self.assertFalse(incomplete["shard_observation_complete"])
        self.assertEqual(incomplete["checkpoint_shard"], "new-owner")
        self.assertFalse(incomplete["shards"]["new-owner"]["coverage_complete"])


if __name__ == "__main__":
    unittest.main()
