"""Run the public archive qualification from a clean checkout using disposable Docker subjects."""

import argparse
import json
import secrets
import subprocess
import time
import uuid
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ANT = "ghcr.io/antflydb/antfly@sha256:5df6e5af4ad41c4f8b6b47497ed4618bb738faeac75525a38fa682cba2565d6d"
PG = "pgvector/pgvector@sha256:ab49524d18df7200b74ac9a475e1db6297fc1eb57da4c214c63d10ecc4b754fa"
OS = "opensearchproject/opensearch@sha256:bbdf65cb3d35c61a8ba495f22c1a42d7d7f0df23412e3419bf1e6cfca3ba4774"
VESPA = "vespaengine/vespa@sha256:e6d3943aef86fd3e57cceefc51d3005a3807f22a254383d348ae9563bbd68d6c"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--client", default="antfly-paper-archive-client")
    p.add_argument(
        "--ocr-weights",
        type=Path,
        help="Florence-2 model directory for the raw object arm",
    )
    p.add_argument(
        "--engines",
        nargs="+",
        choices=["postgres", "antfly", "opensearch", "vespa", "lance"],
        default=["postgres", "antfly", "opensearch", "vespa", "lance"],
    )
    p.add_argument("--corpus", type=Path, help="completed OfficeQA corpus directory")
    p.add_argument("--workload", type=Path, help="compiled OfficeQA tenant workload")
    p.add_argument(
        "--managed-weights",
        type=Path,
        help="root with both BAAI models for true native and durable-outbox migration",
    )
    args = p.parse_args()
    if bool(args.corpus) != bool(args.workload):
        p.error("--corpus and --workload must be supplied together")
    if args.corpus and args.ocr_weights:
        p.error("use corpus.py to prepare the raw-object arm before retrieval")
    args.out.mkdir(parents=True, exist_ok=False)

    def docker(*cmd, **opts):
        return subprocess.run(["docker", *map(str, cmd)], check=True, **opts)

    if subprocess.run(
        ["docker", "image", "inspect", args.client], capture_output=True
    ).returncode:
        docker("build", "-t", args.client, ROOT / "evaluations/retrieval")
    mount = ["-v", f"{ROOT}:/paper:ro", "-v", f"{args.out.resolve()}:/out"]
    base = "/paper/evaluations/archive/"

    def client(network, script, *argv, env=()):
        return docker(
            "run",
            "--rm",
            "--network",
            network,
            *mount,
            *env,
            args.client,
            base + script,
            *argv,
        )

    if args.corpus:
        mount.extend(
            [
                "-v",
                f"{args.corpus.resolve()}:/corpus:ro",
                "-v",
                f"{args.workload.resolve()}:/workload:ro",
            ]
        )
    else:
        client("none", "miniature.py", "/out/matched")
        client("none", "fixture_archive.py", "/out/fixture")
    password = secrets.token_hex(20)
    for engine in args.engines:
        with ExitStack() as stack:

            def start(network, *args):
                name = "paper-archive-" + uuid.uuid4().hex[:16]
                docker("run", "--rm", "-d", "--name", name, "--network", network, *args)
                stack.callback(
                    subprocess.run,
                    ["docker", "stop", "-t", "2", name],
                    capture_output=True,
                )
                return name

            env = []
            network = "none"
            endpoint = "/out/lance-data"
            if engine == "postgres":
                server = start("none", "-e", "POSTGRES_PASSWORD=" + password, PG)
                endpoint = f"postgresql://postgres:{password}@127.0.0.1/postgres"
                port = 5432
            if engine == "antfly":
                server = start(
                    "none",
                    "-e",
                    "ANTFLY_BOOTSTRAP_ADMIN_PASSWORD=" + password,
                    ANT,
                    "standalone",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "8080",
                    "--health",
                    "false",
                    "--auth",
                    "true",
                    "--data-dir",
                    "/tmp/archive",
                )
                endpoint = "http://127.0.0.1:8080"
                port = 8080
                env = ["-e", "ARCHIVE_ANTFLY_AUTH=admin:" + password]
            if engine == "opensearch":
                server = start(
                    "none",
                    "-e",
                    "discovery.type=single-node",
                    "-e",
                    "DISABLE_SECURITY_PLUGIN=true",
                    "-e",
                    "DISABLE_INSTALL_DEMO_CONFIG=true",
                    "-e",
                    "OPENSEARCH_JAVA_OPTS=-Xms1g -Xmx1g",
                    OS,
                )
                endpoint = "http://127.0.0.1:9200"
                port = 9200
            if engine == "vespa":
                server = start(
                    "none",
                    "--hostname",
                    "localhost",
                    "--ulimit",
                    "nofile=65536:524288",
                    VESPA,
                )
                env = ["-e", "ARCHIVE_VESPA_CONFIG=http://127.0.0.1:19071"]
                endpoint = "http://127.0.0.1:8080"
                port = 19071
            if engine != "lance":
                network = "container:" + server
                probe = f"import socket;socket.create_connection(('127.0.0.1',{port}),2).close()"
                if engine == "vespa":
                    probe = "import json,urllib.request as u; assert json.load(u.urlopen('http://127.0.0.1:19071/state/v1/health'))['status']['code']=='up'"
                if engine == "opensearch":
                    probe = "import urllib.request as u; u.urlopen('http://127.0.0.1:9200/_cluster/health?wait_for_status=yellow',timeout=5)"
                for _ in range(120):
                    r = subprocess.run(
                        [
                            "docker",
                            "run",
                            "--rm",
                            "--network",
                            network,
                            args.client,
                            "-c",
                            probe,
                        ],
                        capture_output=True,
                    )
                    if r.returncode == 0:
                        break
                    time.sleep(1)
                else:
                    raise TimeoutError(engine + " readiness")
            client(
                network,
                "compare.py",
                engine,
                "--endpoint",
                endpoint,
                "--corpus",
                "/corpus" if args.corpus else "/out/matched",
                "--workload",
                "/workload" if args.workload else "/out/matched",
                "--out",
                "/out/" + engine,
                "--k",
                "20" if args.corpus else "1",
                "--depth",
                "100" if args.corpus else "5",
                env=env,
            )
            if args.managed_weights and engine in ("antfly", "postgres"):
                model_mount = []
                for model in ("BAAI/bge-small-en-v1.5", "BAAI/bge-small-en"):
                    path = (args.managed_weights / model).resolve(strict=True)
                    model_mount.extend(["-v", f"{path}:/models/{model}:ro"])
                start(
                    network,
                    *model_mount,
                    ANT,
                    "inference",
                    "run",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8092",
                    "--models-dir",
                    "/models",
                )
                model_url = "http://127.0.0.1:8092"
                managed_endpoint = endpoint
                token = secrets.token_hex(24)
                managed_env = [
                    "-e",
                    "PAPER_PASSWORD=" + password,
                    "-e",
                    "PAPER_TOKEN=" + token,
                ]
                gateway = "http://127.0.0.1:8093"
                start(
                    network,
                    *mount,
                    *managed_env,
                    "-e",
                    "PAPER_NATIVE_URL=" + model_url,
                    "-e",
                    "PAPER_GATEWAY_PORT=8093",
                    args.client,
                    "/paper/evaluations/head-to-head/scripts/gateway.py",
                )
                if engine == "postgres":
                    start(
                        network,
                        *mount,
                        *managed_env,
                        "-e",
                        "PAPER_DB_URL=" + endpoint,
                        "-e",
                        "PAPER_MODEL_URL=" + gateway,
                        "-e",
                        "PAPER_WORKER_BATCH=32",
                        args.client,
                        "/paper/evaluations/head-to-head/scripts/pipeline.py",
                    )
                    managed_endpoint = "http://127.0.0.1:8091"
                for _ in range(60):
                    probe = "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8092/ai/v1/models',timeout=2)"
                    if engine == "postgres":
                        probe += ";import socket;socket.create_connection(('127.0.0.1',8091),2).close()"
                    ok = subprocess.run(
                        [
                            "docker",
                            "run",
                            "--rm",
                            "--network",
                            network,
                            args.client,
                            "-c",
                            probe,
                        ],
                        capture_output=True,
                    )
                    if not ok.returncode:
                        break
                    time.sleep(1)
                else:
                    raise TimeoutError("managed migration readiness")
                client(
                    network,
                    "managed.py",
                    engine,
                    "--corpus",
                    "/corpus" if args.corpus else "/out/matched",
                    "--out",
                    "/out/" + engine + "-managed",
                    "--models",
                    model_url,
                    "--producer-models",
                    gateway,
                    "--gate",
                    gateway,
                    "--deadline",
                    "86400" if args.corpus else "120",
                    "--endpoint",
                    managed_endpoint,
                    env=managed_env,
                )
            if args.corpus:
                continue
            result = json.loads((args.out / engine / "result.json").read_text())
            if any(
                r["quality"]["recall"] != 1
                for r in result["results"].values()
                if "quality" in r
            ):
                raise AssertionError(engine + " missed a planted page")
            if engine == "antfly":
                client(
                    network,
                    "filter_profile.py",
                    "--endpoint",
                    endpoint,
                    "--out",
                    "/out/numeric-filter.json",
                    env=env,
                )
            if engine in ("postgres", "antfly"):
                graph_args = [
                    engine,
                    "--endpoint",
                    endpoint,
                    "--fixture",
                    "/out/fixture",
                ]
                client(
                    network,
                    "graph.py",
                    *graph_args,
                    "--out",
                    "/out/" + engine + "-graph.json",
                    env=env,
                )
                if args.ocr_weights:
                    start(
                        network,
                        *mount,
                        args.client,
                        base + "objects.py",
                        "/out/fixture",
                    )
                    start(
                        network,
                        "-v",
                        f"{args.ocr_weights.resolve()}:/models/antflydb/florence-2-base:ro",
                        ANT,
                        "inference",
                        "run",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "8090",
                        "--models-dir",
                        "/models",
                    )
                    for _ in range(60):
                        r = subprocess.run(
                            [
                                "docker",
                                "run",
                                "--rm",
                                "--network",
                                network,
                                args.client,
                                "-c",
                                "import urllib.request as u;u.urlopen('http://127.0.0.1:8090/ai/v1/models',timeout=2)",
                            ],
                            capture_output=True,
                        )
                        if not r.returncode:
                            break
                        time.sleep(1)
                    else:
                        raise TimeoutError("OCR readiness")
                    client(
                        network,
                        "graph.py",
                        *graph_args,
                        "--objects",
                        "http://127.0.0.1:8099",
                        "--models",
                        "http://127.0.0.1:8090",
                        "--out",
                        "/out/" + engine + "-raw.json",
                        env=env,
                    )


if __name__ == "__main__":
    main()
