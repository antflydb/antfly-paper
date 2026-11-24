"""Measure full-size database costs with frozen B vectors in two opposite trial orders."""

import argparse
import json
import subprocess
from pathlib import Path

from fairness_control import subject

ACCESS = {
    "antfly_basic": "default",
    "opensearch_policy": "dls",
    "antfly_policy": "api_key",
    "opensearch_trusted": "default",
    "antfly_trusted": "application",
}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("corpus", "workload", "out"):
        p.add_argument("--" + key, required=True, type=Path)
    p.add_argument("--probe", type=int, required=True)
    p.add_argument("--modes", nargs="+", choices=list(ACCESS), default=list(ACCESS))
    p.add_argument("--rounds", type=int, choices=(1, 2), default=2)
    args = p.parse_args()
    if len(args.modes) != len(set(args.modes)):
        p.error("duplicate modes")
    args.out.mkdir(parents=True, exist_ok=False)
    mounts = []
    for path, target in (
        (args.corpus, "/corpus:ro"),
        (args.workload, "/workload:ro"),
        (args.out, "/out"),
    ):
        mounts.extend(["-v", f"{path.resolve()}:{target}"])
    failed = False
    for i in range(args.rounds):
        for mode in args.modes if i == 0 else args.modes[::-1]:
            name = f"round-{i + 1}-{mode}"
            try:
                with subject(mode, mounts) as (client, env):
                    engine = mode.split("_")[0]
                    base = "http://127.0.0.1:" + (
                        "8080" if engine == "antfly" else "9200"
                    )
                    subprocess.run(
                        [
                            *client,
                            "-c",
                            "import sys; sys.path.insert(0,'/paper/evaluations/archive'); from fairness_control import wait_for_subject; wait_for_subject(sys.argv[1])",
                            mode,
                        ],
                        env=env,
                        check=True,
                    )
                    r = subprocess.run(
                        [
                            *client,
                            "/paper/evaluations/archive/compare.py",
                            engine,
                            "--endpoint",
                            base,
                            "--access",
                            ACCESS[mode],
                            "--corpus",
                            "/corpus",
                            "--workload",
                            "/workload",
                            "--out",
                            "/out/" + name,
                            "--readiness-probe-ordinal",
                            str(args.probe),
                        ],
                        env=env,
                    )
                status = r.returncode
            except (OSError, subprocess.CalledProcessError) as exc:
                print(name, type(exc).__name__, flush=True)
                status = 1
            receipt = args.out / name / "result.json"
            if receipt.exists():
                d = json.loads(receipt.read_text())
                print(
                    name,
                    d["rows_per_generation"],
                    d["b_ready_seconds"],
                    "seconds; exit",
                    status,
                    flush=True,
                )
            else:
                status = status or 1
                print(name, "no final receipt; exit", status, flush=True)
            (args.out / (name + ".exit")).write_text(str(status) + "\n")
            failed |= status != 0
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
