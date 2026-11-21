"""Run the five shared-input subjects and the native Antfly PDF migration serially."""

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from urllib.request import urlopen

from dataset import sha256_file
from engines import call
from native_documents import publish
from qualify import OS, PG, VESPA

SCRIPTS = Path(__file__).resolve().parent
ENGINES = ("antfly", "lance", "postgres", "opensearch", "vespa", "antfly-native")


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=45)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def run_subject(args, engine, out, state):
    out.mkdir(parents=True)
    state.mkdir(parents=True)
    password = secrets.token_hex(24)
    env = {
        **os.environ,
        "PGPASSWORD": password,
        "POSTGRES_PASSWORD": password,
        "ANTFLY_BOOTSTRAP_ADMIN_PASSWORD": password,
        "ARCHIVE_ANTFLY_AUTH": "admin:" + password,
    }
    p = port()
    url = f"http://127.0.0.1:{p}"
    native = engine == "antfly-native"
    options = []
    with ExitStack() as stack:
        log = stack.enter_context((out / "server.log").open("ab"))

        def start(*cmd):
            proc = subprocess.Popen(
                list(map(str, cmd)), stdout=log, stderr=log, env=env
            )
            stack.callback(stop, proc)
            return proc

        if engine in ("antfly", "antfly-native"):
            config = {
                "remote_content": {
                    "security": {
                        "allowed_hosts": ["127.0.0.1"],
                        "block_private_ips": False,
                    }
                },
                "public_inference": {"enabled": native},
            }
            publish(state / "config.json", config)
            scratch_mib = 16384 if native else 6144
            publish(out / "inference-budget.json", {"scratch_mib": scratch_mib})
            server = start(
                args.binary,
                "standalone",
                "--host",
                "127.0.0.1",
                "--port",
                p,
                "--health",
                "false",
                "--auth",
                "true",
                "--models-dir",
                args.models_dir,
                "--config",
                state / "config.json",
                "--data-dir",
                state / "db",
                "--inference-scratch-budget-mb",
                scratch_mib,
            )

            def ready():
                if server.poll() is not None:
                    raise ChildProcessError("Antfly exited before readiness")
                call(url + "/db/v1/tables", "GET", auth=env["ARCHIVE_ANTFLY_AUTH"])

            options = ["--access", "api_key"]
        elif engine == "lance":
            url = str(state / "db")

            def ready():
                pass
        else:
            container = "paperpdf" + uuid.uuid4().hex[:12]
            publish_port = ["-p", f"127.0.0.1:{p}:"]
            if engine == "postgres":
                publish_port[-1] += "5432"
                argv = ["--shm-size", "1g", "-e", "POSTGRES_PASSWORD", PG]
                url = f"postgresql://postgres@127.0.0.1:{p}/postgres"

                def ready():
                    import psycopg

                    try:
                        with psycopg.connect(
                            url, password=password, connect_timeout=2
                        ) as c:
                            c.execute("SELECT 1")
                    except psycopg.OperationalError as e:
                        raise ConnectionError("PostgreSQL starting") from e
            elif engine == "opensearch":
                publish_port[-1] += "9200"
                argv = [
                    "-e",
                    "discovery.type=single-node",
                    "-e",
                    "DISABLE_SECURITY_PLUGIN=true",
                    "-e",
                    "DISABLE_INSTALL_DEMO_CONFIG=true",
                    "-e",
                    "OPENSEARCH_JAVA_OPTS=-Xms1g -Xmx1g",
                    OS,
                ]

                def ready():
                    with urlopen(
                        url + "/_cluster/health?wait_for_status=yellow", timeout=5
                    ) as r:
                        if json.load(r)["status"] not in ("yellow", "green"):
                            raise OSError("OpenSearch starting")
            else:
                publish_port[-1] += "8080"
                admin = port()
                while admin == p:
                    admin = port()
                env["ARCHIVE_VESPA_CONFIG"] = f"http://127.0.0.1:{admin}"
                argv = [
                    "--hostname",
                    "localhost",
                    "--ulimit",
                    "nofile=65536:524288",
                    "-p",
                    f"127.0.0.1:{admin}:19071",
                    VESPA,
                ]

                def ready():
                    with urlopen(
                        env["ARCHIVE_VESPA_CONFIG"] + "/state/v1/health", timeout=5
                    ) as r:
                        if json.load(r)["status"]["code"] != "up":
                            raise OSError("Vespa starting")

            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-d",
                    "--name",
                    container,
                    *publish_port,
                    *argv,
                ],
                env=env,
                check=True,
            )
            stack.callback(
                subprocess.run,
                ["docker", "stop", "-t", "5", container],
                env=env,
                stdout=log,
                stderr=log,
            )
            stack.callback(
                subprocess.run,
                ["docker", "logs", "--tail", "200", container],
                env=env,
                stdout=log,
                stderr=log,
            )
        for _ in range(300):
            try:
                ready()
                break
            except (OSError, RuntimeError):
                time.sleep(1)
        else:
            raise TimeoutError(engine + " readiness")
        client = [sys.executable, "-u"]
        if native:
            refs = state / "refs.json"
            sp = port()
            objects = start(
                sys.executable,
                "-u",
                SCRIPTS / "local_objects.py",
                "--data",
                args.data,
                "--manifest",
                refs,
                "--port",
                sp,
            )
            deadline = time.monotonic() + 7200
            while not refs.exists():
                if objects.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("object server readiness")
                time.sleep(1)
            client += [
                SCRIPTS / "pdf_native_archive.py",
                "--refs",
                refs,
                "--probe",
                str(args.probe),
            ]
        else:
            client += [
                SCRIPTS / "compare.py",
                engine,
                "--readiness-probe-ordinal",
                str(args.probe),
                *options,
            ]
        results = out / "rankings"
        cmd = client + [
            "--corpus",
            args.inputs,
            "--workload",
            args.workload,
            "--out",
            results,
            "--endpoint",
            url,
        ]
        with (out / "client.log").open("ab") as client_log:
            code = subprocess.run(
                cmd, env=env, stdout=client_log, stderr=client_log
            ).returncode
        if engine in ("antfly", "antfly-native", "lance"):
            size_cmd = ["du", "-sk", str(state / "db")]
            storage_scope = "allocated database directory, both generations"
        else:
            data_dir = {
                "postgres": "/var/lib/postgresql",
                "opensearch": "/usr/share/opensearch/data",
                "vespa": "/opt/vespa/var/db",
            }[engine]
            size_cmd = ["docker", "exec", container, "du", "-sk", data_dir]
            storage_scope = (
                "allocated database directory, both generations: " + data_dir
            )
        size = subprocess.run(size_cmd, env=env, capture_output=True, text=True)
        storage = {"scope": storage_scope, "allocated_bytes": None}
        if size.returncode == 0:
            storage["allocated_bytes"] = int(size.stdout.split()[0]) * 1024
        else:
            storage["measurement_error"] = size.stderr.strip()
        publish(out / "storage.json", storage)
        if (results / "result.json").is_file():
            score = subprocess.check_output(
                [
                    sys.executable,
                    SCRIPTS / "original_recall.py",
                    "--data",
                    args.data,
                    "--result",
                    results,
                ],
                env=env,
            )
            publish(out / "original-recall.json", json.loads(score))
        if code:
            raise RuntimeError(
                f"{engine} replay exited {code}; see {out / 'client.log'}"
            )
        if not (out / "original-recall.json").exists():
            raise ValueError("missing original-question scores")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("binary", "models-dir", "data", "workload", "inputs", "out", "state"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--engines", nargs="+", choices=ENGINES, default=ENGINES)
    parser.add_argument(
        "--probe",
        type=int,
        default=284,
        help="one-based frozen evolved readiness probe",
    )
    args = parser.parse_args()
    manifest = json.loads((args.inputs / "manifest.json").read_text())
    if sha256_file(args.binary) != manifest["subject"]:
        raise ValueError("binary differs from preparation")
    if not subprocess.check_output(
        [args.binary, "--version"], text=True, stderr=subprocess.STDOUT
    ).startswith("antfly 0.3.2 "):
        raise ValueError("expected the pinned v0.3.2 runtime")
    if (
        sha256_file(args.data / "dataset-manifest.json")
        != manifest["acquisition_sha256"]
    ):
        raise ValueError("corpus changed")
    for name, digest in manifest["workload_sha256"].items():
        if sha256_file(args.workload / name) != digest:
            raise ValueError("workload changed")
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Validate all prepared shards before each group of subjects consumes them.
        shards = sorted((args.inputs / "receipts").glob("*.json"))
        if len(shards) != manifest["counts"]["documents"]:
            raise ValueError("incomplete preparation receipts")
        fingerprint = hashlib.sha256()
        for path in [
            args.inputs / "manifest.json",
            args.inputs / "queries.json",
            *shards,
        ]:
            fingerprint.update(bytes.fromhex(sha256_file(path)))
        for receipt in shards:
            expected = json.loads(receipt.read_text())["sha256"]
            for g in ("a", "b"):
                shard = args.inputs / g / "documents" / (receipt.stem + ".jsonl")
                if sha256_file(shard) != expected[g]:
                    raise ValueError("changed vector shard " + str(shard))
        expected_shards = {receipt.stem for receipt in shards}
        for g in ("a", "b"):
            if {
                f.stem for f in (args.inputs / g / "documents").glob("*.jsonl")
            } != expected_shards:
                raise ValueError("unexpected vector shard inventory")
        identity = {"prepared_sha256": fingerprint.hexdigest(), "probe": args.probe}
        cohort = args.out / "cohort.json"
        if cohort.exists() and json.loads(cohort.read_text()) != identity:
            raise ValueError("output directory belongs to a different cohort")
        publish(cohort, identity)
        failed = False
        for engine in args.engines:
            marker = args.out / (engine + ".json")
            if marker.exists() and json.loads(marker.read_text())["success"]:
                continue
            attempt = engine + "-" + str(time.time_ns())
            outcome = {
                "engine": engine,
                "attempt": attempt,
                "start": time.time(),
                "success": False,
            }
            publish(marker, outcome)
            try:
                run_subject(args, engine, args.out / attempt, args.state / attempt)
            except Exception as exc:
                outcome["error"] = str(exc)
                failed = True
            else:
                outcome["success"] = True
            outcome["end"] = time.time()
            publish(marker, outcome)
            print(json.dumps(outcome), flush=True)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
