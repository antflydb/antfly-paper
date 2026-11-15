"""ARM64 checks for invalid-model diagnostics and descriptor-pressure ingestion."""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from http_client import http, wait

if "--worker" not in sys.argv:
    sys.path.append(str(Path(__file__).resolve().parents[2] / "retrieval/scripts"))
    import runtime


def worker():
    import numpy as np

    out = Path("/output")
    rng = np.random.default_rng(1682)
    url = "http://antfly:8080/db/v1"
    status, created = http(url + "/tables/pressure", {"num_shards": 1})
    if status not in (200, 201):
        raise ValueError(f"table creation rejected: HTTP {status} {created}")
    status, created = http(
        url + "/tables/pressure/indexes/vec",
        {
            "type": "embeddings",
            "dimension": 768,
            "distance_metric": "cosine",
            "external": True,
        },
    )
    if status not in (200, 201):
        raise ValueError(f"index creation rejected: HTTP {status} {created}")
    total = int(os.environ["PAPER_FD_ROWS"])
    done = 0
    first = None
    for offset in range(0, total, 256):
        vectors = rng.normal(size=(min(256, total - offset), 768)).astype("float32")
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        if first is None:
            first = vectors[0].tolist()
        docs = {
            f"d{offset + i:09d}": {
                "ordinal": offset + i,
                "_embeddings": {"vec": v.tolist()},
            }
            for i, v in enumerate(vectors)
        }
        status, ack = http(
            url + "/tables/pressure/batch",
            {"inserts": docs, "sync_level": "full_index"},
            timeout=120,
        )
        if status not in (200, 201) or ack.get("status") != "committed":
            raise RuntimeError(
                f"ingestion rejected at {done}: HTTP {status} {str(ack)[:500]}"
            )
        if offset == 0:
            source_status, source = http(url + "/tables/pressure/documents/d000000000")
            if source_status != 200 or source.get("ordinal") != 0:
                raise ValueError(
                    "pressure fixture failed its live-source positive control"
                )
        done += len(vectors)
        (out / "progress.json").write_text(
            json.dumps({"committed": done, "total": total}) + "\n"
        )
        if done % 8192 == 0:
            print(json.dumps({"committed": done}), flush=True)
    status, response = http(
        url + "/query",
        {
            "table": "pressure",
            "indexes": ["vec"],
            "embeddings": {"vec": first},
            "fields": ["_id"],
            "limit": 10,
        },
        timeout=120,
    )
    hits = response["responses"][0].get("hits", {}).get("hits", [])
    if status != 200 or len(hits) != 10:
        raise ValueError("post-ingestion query failed")
    (out / "worker-result.json").write_text(
        json.dumps({"committed": done, "query_hits": len(hits)}) + "\n"
    )


def run(args):
    if not runtime.CANDIDATE:
        raise ValueError("fix verification requires a pinned candidate")
    runtime.LABEL = "antfly-paper.fixes=" + args.check
    owned = runtime.Owned(args.out)
    args.out.mkdir(parents=True, exist_ok=False)
    runtime.create_network(owned.network, internal=False)
    checks = []
    success = False
    paths = [
        *Path(__file__).parent.glob("*.py"),
        Path(runtime.__file__),
        runtime.SUBJECT_IMPLEMENTATION,
    ]
    hashes = {str(p.relative_to(runtime.REPOSITORY)): runtime.sha(p) for p in paths}

    def check(name, passed, **details):
        row = {"name": name, "passed": bool(passed), **details}
        checks.append(row)
        print(json.dumps(row), flush=True)
        if not passed:
            raise AssertionError(name)

    try:
        if args.check == "invalid-model":
            if not args.models:
                raise ValueError(
                    "--models must point to the original cached model root"
                )
            model = args.models / "fastino/gliner2-base-v1"
            if not model.is_dir():
                raise ValueError("historical model cache missing")
            name = owned.container(
                "inference",
                runtime.ANTFLY_IMAGE,
                [
                    "inference",
                    "run",
                    "--host",
                    "0.0.0.0",
                    "--allow-insecure-public-bind",
                    "--port",
                    "8090",
                    "--models-dir",
                    "/models",
                ],
                mounts=[
                    f"type=bind,src={model.resolve()},dst=/models/fastino/gliner2-base-v1,readonly"
                ],
                ports=[8090],
                cpus=3,
                memory="8g",
            )
            url = f"http://127.0.0.1:{owned.port(name, 8090)}"

            def ready():
                try:
                    return http(url + "/ai/v1/models")[0] == 200
                except OSError:
                    return False

            wait(ready)
            for version in (None, 2):
                request = {
                    "model": "fastino/gliner2-base-v1",
                    "schema": {"entities": ["email"]},
                    "inputs": [{"content": "Contact custodian0@example.invalid."}],
                }
                if version:
                    request["schema_version"] = version
                status, body = http(url + "/ai/v1/extract", request)
                check(
                    "invalid-cache-" + ("explicit-v2" if version else "legacy-request"),
                    status == 400
                    and body.get("error") == "INVALID_MODEL"
                    and any(
                        word in body.get("message", "").lower()
                        for word in ("encoder", "config", "pull")
                    ),
                    status=status,
                    response=body,
                )
            check("discovery-remains-available", http(url + "/ai/v1/models")[0] == 200)
            (args.out / "model-files.json").write_text(
                json.dumps(
                    {
                        str(p.relative_to(model)): runtime.sha(p)
                        for p in model.rglob("*")
                        if p.is_file() and not p.name.endswith(".antfly-sha256-cache")
                    },
                    indent=2,
                )
                + "\n"
            )
        else:
            volume = owned.volume("database")
            config = args.out / "config.json"
            config.write_text(
                json.dumps({"inference": {"api_url": "http://127.0.0.1:1"}})
            )
            name = owned.container(
                "antfly",
                runtime.ANTFLY_IMAGE,
                [
                    "standalone",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "8080",
                    "--auth",
                    "false",
                    "--health",
                    "false",
                    "--data-dir",
                    "/antflydb",
                    "--config",
                    "/config.json",
                ],
                mounts=[
                    f"type=volume,src={volume},dst=/antflydb",
                    f"type=bind,src={config},dst=/config.json,readonly",
                ],
                ports=[8080],
                cpus=4,
                memory="12g",
                ulimits=["nofile=1024:524288"],
            )
            url = f"http://127.0.0.1:{owned.port(name, 8080)}"

            def ready():
                try:
                    return http(url + "/db/v1/tables")[0] == 200
                except OSError:
                    return False

            wait(ready)
            owned.environment["PAPER_FD_ROWS"] = str(args.rows)
            job = owned.client(
                "workload",
                ["/native/fixes.py", "--worker"],
                env=["PAPER_FD_ROWS"],
                mounts=[
                    f"type=bind,src={Path(__file__).parent},dst=/native,readonly",
                    f"type=bind,src={runtime.CAMPAIGN / 'scripts'},dst=/retrieval/scripts,readonly",
                    f"type=bind,src={args.out},dst=/output",
                ],
                cpus=1,
                memory="1g",
            )
            # No host Docker socket is given to the worker.
            snapshots = []
            deadline = time.monotonic() + 3600
            while time.monotonic() < deadline:
                state = json.loads(runtime.docker(["inspect", job]))[0]["State"]
                if not state["Running"]:
                    break
                sample = runtime.docker(
                    ["exec", name, "sh", "-c", "ls /proc/1/fd | wc -l"]
                ).strip()
                snapshots.append({"time": time.time(), "descriptors": int(sample)})
                time.sleep(5)
            else:
                raise TimeoutError("descriptor pressure gate deadline")
            (args.out / "descriptor-samples.json").write_text(
                json.dumps(snapshots, indent=2) + "\n"
            )
            check(
                "low-limit-ingestion-process-completed",
                state["ExitCode"] == 0,
                worker_exit_code=state["ExitCode"],
                worker_log_tail=owned.logs(job)[-2000:],
            )
            result = json.loads((args.out / "worker-result.json").read_text())
            check(
                "low-limit-full-ingestion",
                result["committed"] == args.rows and result["query_hits"] == 10,
                rows=args.rows,
                nofile="1024:524288",
                sampled_peak=max((s["descriptors"] for s in snapshots), default=None),
            )
        success = True
    except Exception as exc:
        (args.out / "failure.json").write_text(
            json.dumps(
                {"error": type(exc).__name__, "message": str(exc)[:1000]}, indent=2
            )
            + "\n"
        )
        raise
    finally:
        runtime.write_records(
            args.out,
            {
                "protocol": "main-fixes-v1",
                "check": args.check,
                "status": "completed" if success else "execution-incomplete",
                "checks": checks,
            },
            {
                "subject": runtime.subject_provenance(),
                "source_hashes": hashes,
                "source_unchanged": hashes
                == {
                    str(p.relative_to(runtime.REPOSITORY)): runtime.sha(p)
                    for p in paths
                },
            },
        )
        owned.close(success)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--worker", action="store_true")
    p.add_argument("--check", choices=["invalid-model", "descriptors"])
    p.add_argument("--out", type=Path)
    p.add_argument("--models", type=Path)
    p.add_argument("--rows", type=int, default=100000)
    args = p.parse_args()
    if args.worker:
        worker()
    elif not args.check or not args.out:
        p.error("--check and --out are required")
    else:
        args.out = args.out.resolve()
        run(args)
