"""Validate that the negative controls can detect real provider differences."""

import json
import threading
import time
import unittest

from model import CANARY, Model


class ModelTests(unittest.TestCase):
    def test_decisions_cannot_accidentally_reproduce(self):
        model = Model()
        body = {"messages": [{}, {"content": json.dumps({"items": [{"id": 0}]})}]}
        one = model.infer("/v1/chat/completions", body)
        two = model.infer("/v1/chat/completions", body)
        self.assertNotEqual(
            one["choices"][0]["message"]["content"],
            two["choices"][0]["message"]["content"],
        )
        model.decision_fail = True
        with self.assertRaises(TimeoutError):
            model.infer("/v1/chat/completions", body)

    def test_wrong_and_missing_identity_are_actual_wire_differences(self):
        model = Model()
        one = model.infer("/embed", {"input": ["same source"]})
        model.revision = "B"
        two = model.infer("/embed", {"input": ["same source"]})
        self.assertNotEqual(one["data"][0]["embedding"], two["data"][0]["embedding"])
        self.assertEqual(two["model_identity"], "b" * 64)
        model.ack = False
        self.assertNotIn(
            "model_identity", model.infer("/embed", {"input": ["same source"]})
        )

    def test_exposure_counter_has_positive_and_negative_controls(self):
        model = Model()
        model.infer("/embed", {"input": [CANARY]})
        model.infer("/embed", {"input": ["[EMAIL]"]})
        self.assertEqual([c["canary_matches"] for c in model.calls], [1, 0])
        self.assertIn(
            __import__("hashlib").sha256(CANARY.encode()).hexdigest(),
            model.calls[0]["inputs_sha256"],
        )
        self.assertNotIn(CANARY, str(model.calls))

    def test_detector_is_proven_held_before_release(self):
        model = Model()
        model.detector_hold = True
        body = {
            "response_format": {
                "json_schema": {"schema": {"properties": {"findings": {}}}}
            }
        }
        result = []
        thread = threading.Thread(
            target=lambda: result.append(model.infer("/chat/completions", body))
        )
        thread.start()
        deadline = time.monotonic() + 2
        while not model.pending and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertTrue(model.pending)
        self.assertFalse(result)
        with model.lock:
            for gate in model.pending.values():
                gate.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(model.calls[0]["task"], "detector")
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
