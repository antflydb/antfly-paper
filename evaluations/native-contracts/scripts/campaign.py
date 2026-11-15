"""Sequential, gated main-candidate qualification and alternating fresh trials."""

import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def main(args):
    if not os.environ.get("PAPER_SUBJECT_MANIFEST"):
        raise ValueError("explicit pinned candidate required")
    args.out.mkdir(parents=True, exist_ok=False)
    records = []
    source = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT):
        raise ValueError("freeze the instruments before starting the campaign")
    manifest = Path(os.environ["PAPER_SUBJECT_MANIFEST"]).read_bytes()

    def run(name, script, extra=(), timeout=21600):
        out = args.out / name
        command = [
            sys.executable,
            str(ROOT / "evaluations" / script),
            *map(str, extra),
            "--out",
            str(out),
        ]
        began = time.monotonic()
        row = {
            "name": name,
            "command": command,
            "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        print(json.dumps(row), flush=True)
        try:
            with (args.out / (name + ".log")).open("w") as log:
                completed = subprocess.run(
                    command,
                    cwd=ROOT,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=timeout,
                    check=False,
                )
            row["exit_code"] = completed.returncode
            result = json.loads((out / "result.json").read_text())
            provenance = json.loads((out / "provenance.json").read_text())
            receipt = json.loads((out / "receipt.json").read_text())["sha256"]
            verified = all(
                receipt.get(name)
                == hashlib.sha256((out / name).read_bytes()).hexdigest()
                for name in ("result.json", "provenance.json")
            )
            unchanged = [
                document[key]
                for document in (result, provenance)
                for key in ("source_unchanged", "source_unchanged_during_run")
                if key in document
            ]
            row["passed"] = (
                completed.returncode == 0
                and result.get("status") == "completed"
                and verified
                and bool(unchanged)
                and all(value is True for value in unchanged)
                and bool(result.get("checks"))
                and all(c["passed"] for c in result["checks"])
            )
        except Exception as exc:
            row.update(passed=False, error=type(exc).__name__)
        row["elapsed_seconds"] = time.monotonic() - began
        records.append(row)
        (args.out / "campaign.json").write_text(
            json.dumps(
                {
                    "source_commit": source,
                    "subject_manifest_sha256": hashlib.sha256(manifest).hexdigest(),
                    "runs": records,
                },
                indent=2,
            )
            + "\n"
        )
        print(json.dumps(row), flush=True)
        return row["passed"]

    gates = [
        run("native-contracts", "native-contracts/scripts/run.py"),
        run(
            "descriptors",
            "native-contracts/scripts/fixes.py",
            ["--check", "descriptors", "--rows", "100000"],
        ),
        run("extended", "lifecycle-extended/scripts/run.py"),
    ]
    for engine in ("antfly", "postgres-outbox", "lancedb", "opensearch", "vespa"):
        passed = run("core-" + engine, "lifecycle/scripts/run.py", ["--engine", engine])
        if engine == "antfly":
            gates.append(passed)
    common = [
        "--data",
        args.data,
        "--models",
        args.models,
        "--migration-models",
        args.migration_models,
    ]
    for engine in ("antfly", "postgres"):
        gates.append(
            run(
                "qualification-" + engine,
                "head-to-head/scripts/run.py",
                ["--engine", engine, *common],
            )
        )
    if all(gates) and not args.qualification_only:
        for trial in range(1, 4):
            order = ("antfly", "postgres") if trial % 2 else ("postgres", "antfly")
            for engine in order:
                run(
                    f"trial-{trial}-{engine}",
                    "head-to-head/scripts/run.py",
                    [
                        "--engine",
                        engine,
                        *common,
                        "--performance",
                        "--copies",
                        "128",
                        "--duration",
                        "60",
                        "--rate",
                        "2",
                    ],
                )
    else:
        print(
            json.dumps(
                {
                    "timing_not_run": "qualification-only requested"
                    if args.qualification_only
                    else "required qualification gate failed"
                }
            ),
            flush=True,
        )
    return 0 if all(r["passed"] for r in records) else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--models", type=Path, required=True)
    p.add_argument("--migration-models", type=Path, required=True)
    p.add_argument("--qualification-only", action="store_true")
    raise SystemExit(main(p.parse_args()))
