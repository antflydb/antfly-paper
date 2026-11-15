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


model.embedding = embedding
if __name__ == "__main__":
    model.serve().serve_forever()
