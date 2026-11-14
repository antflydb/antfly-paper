"""Cross-check prepared wiring truth using scalar cosine and a full sort."""

import argparse
import json
import math
from pathlib import Path

import numpy as np

parser = argparse.ArgumentParser()
parser.add_argument("--data", type=Path, required=True)
args = parser.parse_args()
vectors = np.load(args.data / "vectors.npy", mmap_mode="r")
queries = np.load(args.data / "queries.npy")
permissions = np.load(args.data / "permissions.npy")
truth = np.load(args.data / "truth.npy")
assert len(vectors) == 10000, "this cross-check is for the wiring corpus"
checked = 0
for cell in range(8):
    eligible = np.flatnonzero(permissions & (1 << cell)).tolist()
    assert eligible and len(eligible) == len(set(eligible))
    for query_index in (0, len(queries) - 1):
        query = queries[query_index].tolist()
        qnorm = math.sqrt(math.fsum(x * x for x in query))
        distances = []
        for identity in eligible:
            vector = vectors[identity].tolist()
            norm = math.sqrt(math.fsum(x * x for x in vector))
            dot = math.fsum(x * y for x, y in zip(vector, query, strict=True))
            distances.append((1 - dot / (norm * qnorm), identity))
        expected = [identity for _, identity in sorted(distances)[:10]]
        assert expected == truth[cell, query_index].tolist(), (cell, query_index)
        checked += 1
print(
    json.dumps(
        {
            "status": "passed",
            "cells": 8,
            "queries_per_cell": 2,
            "top10_cross_checks": checked,
            "method": "scalar math.fsum cosine; complete eligible-set sort",
            "scope": "10k oracle wiring; no engine or 1M qualification",
        }
    )
)
