"""Adversarial oracle and provider controls, independent of live engine success."""

import threading
import time
import unittest

from fixture import assess, documents, embedding
from provider import State


class OracleTests(unittest.TestCase):
    def setUp(self):
        self.ledger = documents()
        self.hits = {k: embedding(v["body"], "A") for k, v in self.ledger.items()}

    def test_exact_fixture(self):
        self.assertTrue(assess(self.hits, self.ledger, "A")["passed"])

    def test_empty_or_missing_reads_fail(self):
        self.assertFalse(assess({}, self.ledger, "A")["passed"])
        self.hits.pop("doc0")
        self.assertFalse(assess(self.hits, self.ledger, "A")["passed"])
        with self.assertRaises(ValueError):
            assess({}, {}, "A")

    def test_wrong_model_source_and_orphan_fail(self):
        for vector in [
            embedding(self.ledger["doc0"]["body"], "B"),
            embedding("obsolete source", "A"),
            [float("nan"), 1.0],
        ]:
            hits = {**self.hits, "doc0": vector}
            self.assertFalse(assess(hits, self.ledger, "A")["passed"])
        self.hits["orphan"] = [1, 0]
        self.assertFalse(assess(self.hits, self.ledger, "A")["passed"])


class ProviderTests(unittest.TestCase):
    def test_held_request_keeps_captured_revision(self):
        state = State()
        state.mode = "hold"
        result = []
        worker = threading.Thread(
            target=lambda: result.append(state.embed(["held source"]))
        )
        worker.start()
        deadline = time.monotonic() + 2
        while not state.snapshot()["pending"] and time.monotonic() < deadline:
            time.sleep(0.001)
        with state.lock:
            self.assertTrue(state.pending)
            state.revision = "B"
            state.mode = "ok"
            for gate in state.pending.values():
                gate.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0]["model"], "A")
        self.assertEqual(
            result[0]["data"][0]["embedding"], embedding("held source", "A")
        )
        self.assertEqual(state.snapshot()["calls"][0]["state"], "complete")
        self.assertNotIn("held source", str(state.snapshot()))

    def test_failure_does_not_masquerade_as_embedding(self):
        state = State()
        state.mode = "fail"
        with self.assertRaises(TimeoutError):
            state.embed(["source"])
        self.assertEqual(state.snapshot()["calls"][0]["state"], "failed")


if __name__ == "__main__":
    unittest.main()
