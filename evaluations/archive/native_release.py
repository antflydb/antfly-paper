"""Run corpus retrieval on a disposable loopback Antfly release without Docker."""

import argparse
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
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--corpus", type=Path, required=True)
    p.add_argument("--workload", type=Path, required=True)
    p.add_argument("--migration-models")
    args = p.parse_args()
    version = subprocess.check_output(
        [str(args.binary.resolve()), "--version"], text=True
    )
    if not version.startswith("antfly 0.3.1 "):
        raise ValueError("this campaign uses the official v0.3.1 subject")
    if not (args.corpus / "manifest.json").is_file():
        raise ValueError("corpus preparation has not completed")
    args.state.mkdir(parents=True, exist_ok=False)
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
