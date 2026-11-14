"""Safety/quality regressions: unsafe or short fast responses cannot win tuning."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from adapters import Antfly, cell_number
from retrieval_workload import (
    GlobalExactControls,
    atomic_json,
    postfilter_summary,
    qualifies,
    score,
    select_candidate,
    summarize,
)


class SharedControlTest(unittest.TestCase):
    def test_one_physical_fetch_and_independent_permission_views(self):
        calls = []

        def fetch(query):
            calls.append(query)
            return list(range(10))

        controls = GlobalExactControls(fetch, 10)
        permissions = np.array([1] * 5 + [2] * 5, dtype=np.uint16)
        truth = np.tile(np.arange(10), (8, 1, 1))
        for _ in range(8):
            controls.get(0, "frozen query")
        first = postfilter_summary(controls, [0], truth, permissions, 0)
        second = postfilter_summary(controls, [0], truth, permissions, 1)
        self.assertEqual(calls, ["frozen query"])
        self.assertEqual(controls.records[0]["ids"], tuple(range(10)))
        self.assertEqual(first["completeness"], 0.5)
        self.assertEqual(second["completeness"], 0.5)
        self.assertEqual(first["unauthorized"], 0)
        self.assertEqual(second["unauthorized"], 0)
        self.assertIsNone(first["latency_ms"])

    def test_failure_is_cached_without_retry_or_false_success(self):
        calls = []

        def fetch(query):
            calls.append(query)
            raise TimeoutError("controlled failure")

        controls = GlobalExactControls(fetch, 10)
        for _ in range(8):
            self.assertEqual(controls.get(0, "query")["error"], 1)
        self.assertEqual(len(calls), 1)
        aggregate = postfilter_summary(
            controls,
            [0],
            np.tile(np.arange(10), (8, 1, 1)),
            np.ones(10, dtype=np.uint16),
            0,
        )
        self.assertEqual(aggregate["errors"], 1)
        self.assertFalse(qualifies(aggregate, 0.95))

    def test_progress_replace_is_readable_and_does_not_mutate_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            atomic_json(root / "checkpoint.json", {"completed": 10, "errors": 1})
            atomic_json(
                root / "progress.json", {"completed": 10, "state": "request-in-flight"}
            )
            atomic_json(
                root / "progress.json", {"completed": 11, "state": "query-completed"}
            )
            self.assertEqual(
                json.loads((root / "progress.json").read_text())["completed"], 11
            )
            self.assertEqual(
                json.loads((root / "checkpoint.json").read_text())["errors"], 1
            )
            self.assertFalse(list(root.glob("*.tmp")))


class DefaultProfileTest(unittest.TestCase):
    def test_default_request_omits_effort_and_exact_request_sets_it(self):
        adapter = Antfly.__new__(Antfly)
        bodies = []

        def call(client, method, path, body):
            bodies.append(body)
            return {
                "responses": [
                    {"status": 200, "hits": {"hits": [{"_id": "d000000001"}]}}
                ]
            }

        adapter.call = call
        self.assertEqual(
            adapter.query_as(
                None, np.array([1.0, 0.0]), {"mode": "default", "effort": 0.5}
            ),
            [1],
        )
        self.assertNotIn("search_effort", bodies[-1])
        adapter.query_as(None, np.array([1.0, 0.0]), {"effort": 1.0})
        self.assertEqual(bodies[-1]["search_effort"], 1.0)

    def test_default_profile_has_no_tuning_grid(self):
        with patch.dict(os.environ, {"PAPER_ANTFLY_PROFILE": "default"}):
            self.assertEqual(
                Antfly.__new__(Antfly).candidates(),
                [{"mode": "default", "effort": 0.5}],
            )

    def test_predeclared_default_is_measured_even_when_calibration_is_negative(self):
        default = {"mode": "default", "effort": 0.5}
        self.assertEqual(select_candidate([], predeclared=default), default)


class QualityGateTest(unittest.TestCase):
    def setUp(self):
        self.permissions = np.array([1] * 10 + [0], dtype=np.uint16)
        self.truth = list(range(10))

    def aggregate(self, returned, ms=1):
        row = score(returned, self.truth, self.permissions, 0)
        return summarize([{**row, "error": 0, "ms": ms}])

    def test_starvation_cannot_qualify(self):
        self.assertFalse(qualifies(self.aggregate([0, 1]), 0.95))

    def test_denied_row_and_duplicate_cannot_qualify(self):
        for response in (list(range(1, 11)), [*range(9), 8]):
            self.assertFalse(qualifies(self.aggregate(response), 0.95))

    def test_invalid_identity_or_cell_rejected(self):
        for response in ([-1], [11], [True], ["d000000001"]):
            with self.assertRaises(ValueError):
                self.aggregate(response)
        for cell in (-1, 8, True, "0"):
            with self.assertRaises(ValueError):
                cell_number(cell)

    def test_faster_low_quality_candidate_loses(self):
        selected = select_candidate(
            [
                {"candidate": "fast-but-short", "aggregate": self.aggregate([0], 0.01)},
                {"candidate": "safe", "aggregate": self.aggregate(self.truth, 10)},
                {
                    "candidate": "safe-but-slower",
                    "aggregate": self.aggregate(self.truth, 20),
                },
            ]
        )
        self.assertEqual(selected, "safe")
        self.assertIsNone(
            select_candidate([{"candidate": "bad", "aggregate": self.aggregate([])}])
        )

    def test_request_error_counts_against_acceptance(self):
        row = {
            **score(self.truth, self.truth, self.permissions, 0),
            "error": 1,
            "ms": 1,
        }
        self.assertFalse(qualifies(summarize([row]), 0.95))


if __name__ == "__main__":
    unittest.main()
