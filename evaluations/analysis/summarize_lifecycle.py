"""Verify lifecycle receipts and committed instruments before reporting controls."""

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINES = {"antfly", "postgres-outbox", "lancedb", "vespa", "opensearch"}
BASE = {"initial-derived-ready", "metadata-only-reuse", "migration-b1", "migration-a2"}
ASYNC = {
    "held-source-update",
    "held-source-delete",
    "after-publication-crash",
    "crash-with-request-held",
}


def sha_bytes(value):
    return hashlib.sha256(value).hexdigest()


def sha(path):
    return sha_bytes(path.read_bytes())


def verify(directory, engine, revision):
    receipt = json.loads((directory / "receipt.json").read_text())
    for name in ["result.json", "provenance.json"]:
        if receipt["sha256"].get(name) != sha(directory / name):
            raise ValueError(engine + ": receipt mismatch for " + name)
    result = json.loads((directory / "result.json").read_text())
    provenance = json.loads((directory / "provenance.json").read_text())
    if (
        result.get("protocol") != "lifecycle-v1"
        or result.get("engine") != engine
        or result.get("status") != "completed"
    ):
        raise ValueError(engine + ": incompatible or incomplete lifecycle")
    if provenance.get("source_unchanged_during_run") is not True:
        raise ValueError(engine + ": source was not frozen")
    for path, expected in provenance["scripts_sha256"].items():
        content = subprocess.check_output(
            ["git", "show", f"{revision}:{path}"], cwd=ROOT
        )
        if sha_bytes(content) != expected:
            raise ValueError(
                engine + ": instrument does not match declared commit: " + path
            )
    checks = result.get("checks", [])
    names = [x["name"] for x in checks]
    required = BASE | (
        ASYNC
        if engine in {"antfly", "postgres-outbox"}
        else {"synchronous-update-delete-restart"}
    )
    if (
        len(names) != len(set(names))
        or not required <= set(names)
        or any(x.get("passed") is not True for x in checks)
    ):
        raise ValueError(engine + ": missing, duplicate or failed required checks")
    observations = result["observations"]
    if observations.get("scored_queries", 0) <= 0 or any(
        observations.get(k) != 0
        for k in ["post_cutover_vector_mismatches", "post_barrier_stale_hits"]
    ):
        raise ValueError(engine + ": no observations or nonzero violations")
    return result, provenance, receipt


def collect(sources, revision):
    if set(sources) != ENGINES:
        raise ValueError(
            "one result from each of the five declared engines is required"
        )
    if not re.fullmatch("[0-9a-f]{40}", revision):
        raise ValueError("full immutable source commit required")
    rows = {}
    fixtures = set()
    requirements = set()
    for engine, directory in sorted(sources.items()):
        result, provenance, receipt = verify(directory, engine, revision)
        fixtures.add(result["fixture_sha256"])
        requirements.add(provenance["requirements_sha256"])
        rows[engine] = {
            "subject": provenance["engine_subject"],
            "checks_passed": len(result["checks"]),
            "observations": result["observations"],
            "not_run": result["not_run"],
            "input_sha256": receipt["sha256"],
        }
    if len(fixtures) != 1 or len(requirements) != 1:
        raise ValueError("mixed fixture or dependency definitions")
    return {
        "protocol": "lifecycle-v1",
        "status": "verified-controls",
        "source_commit": revision,
        "scope": "eight synthetic documents; correctness controls only; no real-model, application-effort or latency ranking",
        "fixture_sha256": fixtures.pop(),
        "requirements_sha256": requirements.pop(),
        "engines": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", action="append", required=True, help="ENGINE=DIRECTORY"
    )
    parser.add_argument("--revision", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    pairs = [s.split("=", 1) for s in args.source]
    if any(len(p) != 2 for p in pairs) or len({p[0] for p in pairs}) != len(pairs):
        raise ValueError("duplicate or malformed source label")
    result = collect({k: Path(v) for k, v in pairs}, args.revision)
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    report = [
        "# Verified synthetic lifecycle controls",
        "",
        result["scope"] + ".",
        "",
        f"Source: `{args.revision}`.",
        "",
        "| Engine | Passed controls | Scored queries | Post-cutover vector mismatches |",
        "| --- | ---: | ---: | ---: |",
    ]
    for engine, row in result["engines"].items():
        report.append(
            f"| {engine} | {row['checks_passed']} | {row['observations']['scored_queries']} | {row['observations']['post_cutover_vector_mismatches']} |"
        )
    report += [
        "",
        "Antfly and the custom PostgreSQL outbox exercise asynchronous held-request races.",
        "LanceDB, Vespa and OpenSearch exercise their synchronous native paths;",
        "their smaller check count is a different contract, not an architecture ranking.",
        "",
        "Precise internal crash failpoints, migration overlap faults, heterogeneous producer DAGs,",
        "real-model quality/performance, privacy inventory and authorization attacks remain separate work.",
        "The source results retain their complete not-run lists and pinned subjects.",
        "",
    ]
    (args.out / "report.md").write_text("\n".join(report))
    (args.out / "receipt.json").write_text(
        json.dumps(
            {
                "sha256": {
                    name: sha(args.out / name) for name in ["result.json", "report.md"]
                }
            },
            indent=2,
        )
        + "\n"
    )
    print(
        json.dumps(
            {
                "status": "verified-controls",
                "engines": len(result["engines"]),
                "out": str(args.out),
            }
        )
    )


if __name__ == "__main__":
    main()
