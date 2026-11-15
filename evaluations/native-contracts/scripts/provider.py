"""Orthogonal model spaces; reuse the audited held-provider machinery."""

import sys

sys.path.insert(0, "/extended")
import model


def embedding(text, revision):
    value = (
        [1.0, 0.0]
        if "red" in text
        else [0.0, 1.0]
        if "blue" in text
        else [0.70710678, 0.70710678]
    )
    return value if revision == "A" else [-x for x in value]


class ContractModel(model.Model):
    def infer(self, path, body):
        try:
            return super().infer(path, body)
        except TimeoutError:
            if self.decision_fail and not (
                path.endswith("embed") or path.endswith("embeddings")
            ):
                # The base model records the attempted call first. Make this
                # an explicit permanent rejection rather than a retry budget test.
                raise ValueError("controlled producer rejection") from None
            raise


model.Model = ContractModel
model.embedding = embedding
if __name__ == "__main__":
    model.serve().serve_forever()
