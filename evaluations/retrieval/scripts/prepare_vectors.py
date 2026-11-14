#!/usr/bin/env python3
"""Materialize the common corpus and exact eligible-set oracle before timing."""

import argparse
import hashlib
import json
import time
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

SELECTIVITIES = (0.5, 0.1, 0.01, 0.001)
CELLS = [
    f"{distribution}_{int(rate * 1000):03d}"
    for distribution in ("random", "correlated")
    for rate in SELECTIVITIES
]


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def matrix(batch, field="emb"):
    array = batch.column(batch.schema.get_field_index(field))
    return (
        np.asarray(array.values).reshape(len(array), -1).astype(np.float32, copy=False)
    )


def prepare(train, test, out, size):
    out.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    parquet = pq.ParquetFile(train)
    if size < 10000 or size > parquet.metadata.num_rows or size % 1000:
        raise ValueError("size must be a supported multiple of 1000 >=10000")
    tests = pq.ParquetFile(test).read(columns=["emb"]).combine_chunks()
    query_matrix = (
        np.asarray(tests.column("emb").chunk(0).values)
        .reshape(len(tests), -1)
        .astype(np.float32)
    )
    rng = np.random.default_rng(126)
    selected, fingerprints = [], set()
    for index in rng.permutation(len(query_matrix)):
        digest = hashlib.sha256(query_matrix[index].tobytes()).hexdigest()
        if digest not in fingerprints:
            fingerprints.add(digest)
            selected.append(int(index))
        if len(selected) == 120:
            break
    if len(selected) != 120:
        raise ValueError("not enough distinct queries for calibration and holdout")
    queries = query_matrix[selected]
    dimensions = queries.shape[1]
    np.save(out / "queries.npy", queries)
    vectors = np.lib.format.open_memmap(
        out / "vectors.npy", mode="w+", dtype=np.float32, shape=(size, dimensions)
    )
    offset = 0
    for batch in parquet.iter_batches(batch_size=8192, columns=["emb"]):
        values = matrix(batch)
        count = min(len(values), size - offset)
        vectors[offset : offset + count] = values[:count]
        offset += count
        if offset == size:
            break
    vectors.flush()
    if offset != size or not np.isfinite(queries).all():
        raise ValueError("invalid corpus/query count or nonfinite query")
    query64 = queries.astype(np.float64)
    query_norms = np.linalg.norm(query64, axis=1)
    if np.any(query_norms == 0):
        raise ValueError("zero-norm query")
    anchor_distances = np.lib.format.open_memmap(
        out / "anchor.npy", mode="w+", dtype=np.float64, shape=(size,)
    )
    for start in range(0, size, 8192):
        block = np.asarray(vectors[start : start + 8192], dtype=np.float64)
        norms = np.linalg.norm(block, axis=1)
        if not np.isfinite(block).all() or np.any(norms == 0):
            raise ValueError("nonfinite or zero-norm source vector")
        anchor_distances[start : start + len(block)] = 1 - (block @ query64[0]) / (
            norms * query_norms[0]
        )
    anchor_distances.flush()
    ids = np.arange(size, dtype=np.int64)
    orders = [rng.permutation(size), np.lexsort((ids, anchor_distances))]
    masks = np.lib.format.open_memmap(
        out / "permissions.npy", mode="w+", dtype=np.uint16, shape=(size,)
    )
    masks[:] = 0
    eligibility = {}
    for distribution, order in enumerate(orders):
        for position, rate in enumerate(SELECTIVITIES):
            bit = distribution * 4 + position
            count = round(size * rate)
            masks[order[:count]] |= np.uint16(1 << bit)
            eligibility[CELLS[bit]] = count
    masks.flush()
    best_ids = np.full((8, 120, 10), -1, dtype=np.int64)
    best_distances = np.full((8, 120, 10), np.inf, dtype=np.float64)
    for start in range(0, size, 8192):
        block = np.asarray(vectors[start : start + 8192], dtype=np.float64)
        distances = 1 - (block @ query64.T) / (
            np.linalg.norm(block, axis=1)[:, None] * query_norms[None, :]
        )
        flags = masks[start : start + len(block)]
        for bit in range(8):
            local = np.flatnonzero((flags & (1 << bit)) != 0)
            if not len(local):
                continue
            scores = distances[local]
            for query in range(120):
                values = scores[:, query]
                if len(local) > 10:
                    cutoff = np.partition(values, 9)[9]
                    candidates = np.flatnonzero(values <= cutoff)
                else:
                    candidates = np.arange(len(local))
                row_ids = local[candidates] + start
                order = np.lexsort((row_ids, values[candidates]))[:10]
                candidate_ids = np.concatenate((best_ids[bit, query], row_ids[order]))
                candidate_distances = np.concatenate(
                    (best_distances[bit, query], values[candidates][order])
                )
                order = np.lexsort((candidate_ids, candidate_distances))[:10]
                best_ids[bit, query] = candidate_ids[order]
                best_distances[bit, query] = candidate_distances[order]
        if start % 131072 == 0:
            print(f"Oracle {min(start + len(block), size)}/{size}", flush=True)
    if np.any(best_ids < 0) or not np.isfinite(best_distances).all():
        raise ValueError("incomplete eligible-set ground truth")
    np.save(out / "truth.npy", best_ids)
    np.save(out / "truth-distances.npy", best_distances)
    manifest = {
        "schema_version": 1,
        "dataset": "VectorDBBench Cohere medium 1M",
        "source_train_sha256": sha(train),
        "source_test_sha256": sha(test),
        "size": size,
        "dimensions": dimensions,
        "seed": 126,
        "query_source_rows": selected,
        "calibration_query_ordinals": list(range(20)),
        "holdout_query_ordinals": list(range(20, 120)),
        "cells": CELLS,
        "eligible_counts": eligibility,
        "canonical_id": "d followed by nine-digit parquet row ordinal",
        "correlation_anchor": 0,
        "oracle": "float64 cosine; ascending canonical ID breaks exact ties",
        "files_sha256": {
            name: sha(out / name)
            for name in (
                "vectors.npy",
                "permissions.npy",
                "queries.npy",
                "truth.npy",
                "truth-distances.npy",
            )
        },
        "driver_sha256": sha(Path(__file__)),
        "elapsed_seconds": time.monotonic() - started,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        json.dumps(
            {key: manifest[key] for key in ("size", "dimensions", "elapsed_seconds")},
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train", type=Path, required=True)
    parser.add_argument("--test", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--size", type=int, default=1000000)
    args = parser.parse_args()
    prepare(args.train, args.test, args.out, args.size)
