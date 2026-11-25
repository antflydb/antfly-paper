"""Run corpus retrieval on a disposable loopback Antfly release without Docker."""

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

from engines import call


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--binary", type=Path, required=True)
    p.add_argument(
        "--expected-version",
        choices=("0.3.1", "0.3.2", "0.3.3"),
        help="require this Antfly release for a particular cohort",
    )
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--corpus", type=Path, required=True)
    p.add_argument("--workload", type=Path, required=True)
    p.add_argument("--migration-models")
    p.add_argument("--b-batch-size", type=int, choices=(128, 512, 2048))
    p.add_argument("--readiness-probe-ordinal", type=int, default=1)
    p.add_argument("--original-only", action="store_true")
    args = p.parse_args()
    version = subprocess.check_output(
        [str(args.binary.resolve()), "--version"], text=True, stderr=subprocess.STDOUT
    )
    fields = version.strip().split()
    if (
        len(fields) < 2
        or fields[0] != "antfly"
        or fields[1] not in {"0.3.1", "0.3.2", "0.3.3"}
    ):
        raise ValueError(f"unexpected archive subject: {version.strip()}")
    if args.expected_version and fields[1] != args.expected_version:
        raise ValueError(f"expected Antfly {args.expected_version}, got {fields[1]}")
    digest = hashlib.sha256()
    with args.binary.open("rb") as binary:
        for block in iter(lambda: binary.read(1024 * 1024), b""):
            digest.update(block)
    if not (args.corpus / "manifest.json").is_file():
        raise ValueError("corpus preparation has not completed")
    args.state.mkdir(parents=True, exist_ok=False)
    (args.state / "subject.json").write_text(
        json.dumps(
            {
                "version": fields[1],
                "version_output": version.strip(),
                "binary_sha256": digest.hexdigest(),
            },
            indent=2,
        )
        + "\n"
    )
    password = secrets.token_hex(24)
    url = f"http://127.0.0.1:{args.port}"
    auth = "admin:" + password
    with (args.state / "server.log").open("w") as log:
        proc = subprocess.Popen(
            [
                str(args.binary.resolve()),
                "standalone",
                "--host",
                "127.0.0.1",
                "--port",
                str(args.port),
                "--health",
                "false",
                "--auth",
                "true",
                "--data-dir",
                str((args.state / "data").resolve()),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            env={**os.environ, "ANTFLY_BOOTSTRAP_ADMIN_PASSWORD": password},
        )
        try:
            for _ in range(90):
                if proc.poll() is not None:
                    raise RuntimeError("release exited before readiness")
                try:
                    call(url + "/db/v1/tables", "GET", auth=auth)
                    break
                except (OSError, RuntimeError):
                    time.sleep(1)
            else:
                raise TimeoutError("standalone ready deadline")
            cmd = [
                sys.executable,
                str(Path(__file__).with_name("compare.py")),
                "antfly",
                "--endpoint",
                url,
                "--corpus",
                str(args.corpus),
                "--workload",
                str(args.workload),
                "--out",
                str(args.out),
            ]
            cmd += ["--readiness-probe-ordinal", str(args.readiness_probe_ordinal)]
            if args.b_batch_size:
                cmd += ["--b-batch-size", str(args.b_batch_size), "--access", "api_key"]
            if args.original_only:
                cmd.append("--original-only")
            if args.migration_models:
                cmd += ["--migration-models", args.migration_models]
            return subprocess.run(
                cmd, env={**os.environ, "ARCHIVE_ANTFLY_AUTH": auth}
            ).returncode
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


if __name__ == "__main__":
    raise SystemExit(main())
