"""One fresh lifecycle, identical frozen corpus/quality scoring for each adapter."""

import argparse
import hashlib
import json
import os
import time
from pathlib import Path

import numpy as np
from adapters import cell_number, connect


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def score(returned, truth, permissions, cell):
    cell_number(cell)
    if not isinstance(returned, list) or any(
        type(i) is not int or not 0 <= i < len(permissions) for i in returned
    ):
        raise ValueError("invalid canonical result IDs")
    if len(returned) > 10:
        raise ValueError("response exceeds top ten")
    unique = set(returned)
    return {
        "recall": len(unique & set(truth)) / 10,
        "completeness": min(len(unique), 10) / 10,
        "unauthorized": sum(not (int(permissions[i]) & (1 << cell)) for i in returned),
        "duplicates": len(returned) - len(unique),
    }


def summarize(rows):
    return {
        "queries": len(rows),
        "recall_at_10": float(np.mean([r["recall"] for r in rows])),
        "completeness": float(np.mean([r["completeness"] for r in rows])),
        "errors": sum(r["error"] for r in rows),
        "unauthorized": sum(r["unauthorized"] for r in rows),
        "duplicates": sum(r["duplicates"] for r in rows),
        "latency_ms": {
            name: float(np.percentile([r["ms"] for r in rows if not r["error"]], p))
            if any(not r["error"] for r in rows)
            else None
            for name, p in (("p50", 50), ("p95", 95))
        },
    }


def qualifies(aggregate, floor):
    return (
        aggregate["queries"] > 0
        and aggregate["recall_at_10"] >= floor
        and aggregate["completeness"] == 1
        and all(aggregate[k] == 0 for k in ("errors", "unauthorized", "duplicates"))
    )


def select_candidate(trials, predeclared=None):
    if predeclared is not None:
        return predeclared
    passing = [trial for trial in trials if qualifies(trial["aggregate"], 0.955)]
    return (
        min(passing, key=lambda trial: trial["aggregate"]["latency_ms"]["p50"])[
            "candidate"
        ]
        if passing
        else None
    )


def atomic_json(path, value):
    payload = json.dumps(value, indent=2, allow_nan=False) + "\n"
    if len(payload.encode()) >= 1048576:
        raise ValueError("checkpoint exceeds retained-file budget")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload)
    temporary.replace(path)


class GlobalExactControls:
    """One physical request per query, including failures; never retry implicitly."""

    def __init__(self, fetch, size):
        self.fetch, self.size, self.records = fetch, size, {}

    def get(self, ordinal, vector):
        if ordinal not in self.records:
            started = time.perf_counter_ns()
            try:
                ids = self.fetch(vector)
                if (
                    not isinstance(ids, list)
                    or len(ids) > 10
                    or any(type(i) is not int or not 0 <= i < self.size for i in ids)
                    or len(set(ids)) != len(ids)
                ):
                    raise ValueError("invalid global result IDs")
                record = {"ids": tuple(ids), "error": 0, "error_type": None}
            except Exception as exc:  # noqa: BLE001 - failed physical request is cached, not retried
                record = {"ids": (), "error": 1, "error_type": type(exc).__name__}
            self.records[ordinal] = {
                **record,
                "ms": (time.perf_counter_ns() - started) / 1e6,
            }
        return self.records[ordinal]


def postfilter_summary(controls, indices, truth, permissions, cell):
    rows = []
    for index in indices:
        source = controls.records[index]
        ids = [i for i in source["ids"] if int(permissions[i]) & (1 << cell)]
        rows.append(
            {
                **score(ids, truth[cell, index].tolist(), permissions, cell),
                "error": source["error"],
                "ms": source["ms"],
            }
        )
    aggregate = summarize(rows)
    aggregate["latency_ms"] = None
    aggregate["timing_scope"] = (
        "derived permission view of shared global_exact_control; not separately timed"
    )
    return aggregate


def run(args):
    manifest = json.loads((args.data / "manifest.json").read_text())
    for name, expected in manifest["files_sha256"].items():
        if checksum(args.data / name) != expected:
            raise ValueError("prepared dataset checksum mismatch: " + name)
    vectors = np.load(args.data / "vectors.npy", mmap_mode="r")
    permissions = np.load(args.data / "permissions.npy", mmap_mode="r")
    queries = np.load(args.data / "queries.npy")
    truth = np.load(args.data / "truth.npy")
    if vectors.shape != (manifest["size"], manifest["dimensions"]) or truth.shape != (
        8,
        120,
        10,
    ):
        raise ValueError("prepared fixture shape mismatch")
    if not args.wiring and manifest["size"] != 1000000:
        raise ValueError("full mode requires the one-million-vector corpus")
    calibration = (
        manifest["calibration_query_ordinals"][:2]
        if args.wiring
        else manifest["calibration_query_ordinals"]
    )
    holdout = (
        manifest["holdout_query_ordinals"][:3]
        if args.wiring
        else manifest["holdout_query_ordinals"]
    )
    if set(calibration) & set(holdout):
        raise ValueError("calibration/holdout overlap")
    adapter = connect(args.engine)
    result = {
        "protocol": os.environ.get("PAPER_PROTOCOL", "paper-default-v030-v4"),
        "engine": args.engine,
        "scope": "wiring-only"
        if args.wiring
        else "full one-million-vector lifecycle; matched separate engine/client caps; remote API boundary",
        "cells": {},
        "manifest_sha256": checksum(args.data / "manifest.json"),
        "calibration_queries": calibration,
        "holdout_queries": holdout,
    }

    def progress(phase, **fields):
        value = {
            "engine": args.engine,
            "phase": phase,
            "updated_at_unix": time.time(),
            **fields,
        }
        atomic_json(args.out.parent / "progress.json", value)
        return value

    def checkpoint(active=None):
        atomic_json(
            args.out.parent / "checkpoint.json",
            {
                "result": result,
                "active_measurement": active,
                "updated_at_unix": time.time(),
            },
        )

    def evaluate(cell, candidate, indices, phase):
        adapter.configure(cell, candidate)
        rows = []
        for number, index in enumerate(indices):
            progress(
                phase,
                cell=manifest["cells"][cell],
                candidate=candidate,
                query_ordinal=index,
                completed=number,
                total=len(indices),
                state="request-in-flight",
                request_started_at_unix=time.time(),
                errors=sum(row["error"] for row in rows),
            )
            start = time.perf_counter_ns()
            try:
                returned = adapter.query(queries[index], cell, candidate)
                elapsed = (time.perf_counter_ns() - start) / 1e6
                observation = score(
                    returned, truth[cell, index].tolist(), permissions, cell
                )
                observation.update(error=0, ms=elapsed)
            except Exception as exc:  # noqa: BLE001 - failed queries must count against quality
                observation = {
                    "recall": 0.0,
                    "completeness": 0.0,
                    "unauthorized": 0,
                    "duplicates": 0,
                    "error": 1,
                    "ms": (time.perf_counter_ns() - start) / 1e6,
                }
                # Bound error evidence; do not dump responses or SQL credentials.
                result.setdefault("query_error_types", {})[type(exc).__name__] = (
                    result.get("query_error_types", {}).get(type(exc).__name__, 0) + 1
                )
                print(
                    json.dumps(
                        {
                            "event": "query-error",
                            "phase": phase,
                            "cell": manifest["cells"][cell],
                            "query_ordinal": index,
                            "error_type": type(exc).__name__,
                        }
                    ),
                    flush=True,
                )
            rows.append(observation)
            active = progress(
                phase,
                cell=manifest["cells"][cell],
                candidate=candidate,
                query_ordinal=index,
                completed=number + 1,
                total=len(indices),
                state="query-completed",
                last_latency_ms=observation["ms"],
                errors=sum(row["error"] for row in rows),
            )
            if (
                (number + 1) % 10 == 0
                or observation["error"]
                or number + 1 == len(indices)
            ):
                checkpoint({**active, "aggregate_so_far": summarize(rows)})
        return summarize(rows)

    try:
        print("Loading " + args.engine, flush=True)
        adapter.progress = lambda **fields: progress("loading", **fields)
        progress("loading", state="started", total_rows=len(vectors))
        started = time.monotonic()
        adapter.load(vectors, permissions)
        result["load_seconds"] = time.monotonic() - started
        print("Building " + args.engine, flush=True)
        adapter.progress = lambda **fields: progress("building", **fields)
        progress("building", state="started")
        started = time.monotonic()
        adapter.build()
        result["build_seconds"] = time.monotonic() - started
        time.sleep(0 if args.wiring else 30)
        print("Loaded and indexed " + args.engine, flush=True)
        choices = adapter.candidates()
        for cell, name in enumerate(manifest["cells"]):
            if hasattr(adapter, "authorized_count"):
                expected = int(np.count_nonzero(permissions & (1 << cell)))
                actual = adapter.authorized_count(cell)
                if expected != actual:
                    raise AssertionError("authorization count mismatch")
            trials = []
            result["cells"][name] = {
                "calibration": trials,
                "selected": None,
                "status": "calibrating",
            }
            for candidate in choices:
                aggregate = evaluate(cell, candidate, calibration, "calibration")
                trials.append(
                    {
                        "candidate": candidate,
                        "aggregate": aggregate,
                        "passes": qualifies(aggregate, 0.955),
                    }
                )
                checkpoint()
            predeclared = next((c for c in choices if c.get("mode") == "default"), None)
            selected = select_candidate(trials, predeclared)
            result["cells"][name] = {
                "calibration": trials,
                "selected": selected,
                "selection_policy": "predeclared server default; calibration informational"
                if predeclared
                else "fastest qualifying calibration median",
                "status": "selected"
                if selected is not None
                else "quality-negative-on-calibration",
            }
            print("Calibrated " + name, flush=True)
            checkpoint()
        # Every cell's selection is finalized before the first holdout call.
        result["selection_frozen_before_holdout"] = True
        for cell, name in enumerate(manifest["cells"]):
            record = result["cells"][name]
            selected = record["selected"]
            if selected is None:
                continue
            adapter.configure(cell, selected)
            for index in range(20):
                progress(
                    "warmup", cell=name, completed=index, total=20, candidate=selected
                )
                adapter.query(
                    queries[calibration[index % len(calibration)]], cell, selected
                )
            record["plan"] = adapter.explain(queries[calibration[0]], cell, selected)
            record["holdout"] = evaluate(cell, selected, holdout, "selected-holdout")
            record["status"] = (
                "quality-passed"
                if qualifies(record["holdout"], 0.95)
                else "quality-negative-on-holdout"
            )
            checkpoint()
            print("Measured selected holdout " + name, flush=True)
        # Keep all diagnostics after all primary timings. No control outcome
        # can change a selected configuration or contaminate the next cell.
        exact = (
            {"mode": "ann", "effort": 1.0}
            if args.engine == "antfly"
            else next(
                (
                    c
                    for c in choices
                    if c.get("mode") == "exact"
                    or c.get("effort") == 1
                    and args.engine == "antfly"
                ),
                None,
            )
        )
        diagnostic_count = int(os.environ.get("PAPER_EXACT_CONTROL_QUERIES", "5"))
        if not 1 <= diagnostic_count <= 100:
            raise ValueError("exact diagnostic count must be between 1 and 100")
        exact_indices = holdout[:diagnostic_count]
        result["eligible_exact_control_query_ordinals"] = exact_indices
        for cell, name in enumerate(manifest["cells"]):
            record = result["cells"][name]
            if exact is not None:
                record["exact_control"] = evaluate(
                    cell, exact, exact_indices, "eligible-exact-control"
                )
                checkpoint()
        controls = GlobalExactControls(adapter.global_top10, len(vectors))
        for number, index in enumerate(holdout):
            progress(
                "global-exact-control",
                completed=number,
                total=len(holdout),
                query_ordinal=index,
                state="request-in-flight",
                request_started_at_unix=time.time(),
            )
            source = controls.get(index, queries[index])
            if source["error"]:
                name = source["error_type"]
                result.setdefault("query_error_types", {})[name] = (
                    result.get("query_error_types", {}).get(name, 0) + 1
                )
                print(
                    json.dumps(
                        {
                            "event": "query-error",
                            "phase": "global-exact-control",
                            "query_ordinal": index,
                            "error_type": name,
                        }
                    ),
                    flush=True,
                )
            successful = [r["ms"] for r in controls.records.values() if not r["error"]]
            result["global_exact_control"] = {
                "physical_queries": len(controls.records),
                "errors": sum(r["error"] for r in controls.records.values()),
                "latency_ms": {
                    key: float(np.percentile(successful, p)) if successful else None
                    for key, p in (("p50", 50), ("p95", 95))
                },
                "reuse": "one immutable result per holdout query, shared across eight permission views; failed requests are not retried",
            }
            active = progress(
                "global-exact-control",
                completed=number + 1,
                total=len(holdout),
                query_ordinal=index,
                state="query-completed",
                errors=result["global_exact_control"]["errors"],
                last_latency_ms=source["ms"],
            )
            checkpoint(active)
        for cell, name in enumerate(manifest["cells"]):
            result["cells"][name]["global_top10_postfilter_ablation"] = (
                postfilter_summary(controls, holdout, truth, permissions, cell)
            )
        checkpoint()
        result["adapter"] = adapter.metadata
        result["status"] = "completed"
        checkpoint()
        progress("completed", state="completed")
        return result
    finally:
        adapter.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--engine",
        choices=("antfly", "pgvector", "pgvector-partial", "paradedb", "lancedb"),
        required=True,
    )
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--wiring", action="store_true")
    args = parser.parse_args()
    with args.out.open("x") as stream:
        json.dump(run(args), stream, indent=2, allow_nan=False)
        stream.write("\n")
