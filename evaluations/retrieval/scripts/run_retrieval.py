"""Launch isolated adapter lifecycles; retain failures, reclaim successful state."""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from runtime import (
    ANTFLY,
    ANTFLY_PROFILE,
    ANTFLY_RUNTIME,
    ANTFLY_SHA,
    CAMPAIGN,
    PGVECTOR_IMAGE,
    POSTGRES_IMAGE,
    ROOT,
    SUBJECT_TAG,
    Owned,
    get_client_image,
    sha,
    write_records,
)

ENGINES = ("antfly", "pgvector", "pgvector-partial", "paradedb", "lancedb")


def lifecycle(engine, data, output, wiring):
    if shutil.disk_usage(ROOT).free < 20 * 1024**3:
        raise OSError(
            "less than 20 GiB host disk free; preserve state and stop before another lifecycle"
        )
    owned = Owned(output)
    owned.start()
    success = False
    try:
        if engine in ("pgvector", "pgvector-partial", "paradedb"):
            if engine == "paradedb":
                owned.postgres()
            else:
                owned.postgres(image=PGVECTOR_IMAGE)
        elif engine == "antfly":
            if SUBJECT_TAG != "v0.3.0" and sha(ANTFLY) != ANTFLY_SHA:
                raise ValueError("Antfly binary checksum mismatch")
            owned.environment["ANTFLY_BOOTSTRAP_ADMIN_PASSWORD"] = owned.password
            config = (owned.output / "antfly-config.json").resolve()
            config.write_text(
                json.dumps({"inference": {"api_url": "http://127.0.0.1:1"}})
            )
            volume = owned.volume("antfly-data")
            owned.container(
                "antfly",
                ANTFLY_RUNTIME,
                [
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
                    "/database",
                    "--config",
                    "/config.json",
                ],
                env=["ANTFLY_BOOTSTRAP_ADMIN_PASSWORD"],
                mounts=[
                    *(
                        [f"type=bind,src={ANTFLY.parent},dst=/subject,readonly"]
                        if SUBJECT_TAG != "v0.3.0"
                        else []
                    ),
                    f"type=bind,src={config},dst=/config.json,readonly",
                    f"type=volume,src={volume},dst=/database",
                ],
                entrypoint="/subject/antfly" if SUBJECT_TAG != "v0.3.0" else None,
            )
            owned.environment["PAPER_ANTFLY_URL"] = "http://antfly:8080"
        else:
            volume = owned.volume("lance-data")
            owned.environment["PAPER_LANCE_DIR"] = "/lance"
            owned.client(
                "lance",
                ["/driver/lance_service.py"],
                env=["PAPER_LANCE_DIR"],
                mounts=[
                    f"type=volume,src={volume},dst=/lance",
                    f"type=bind,src={data.resolve()},dst=/corpus,readonly",
                ],
                cpus=4,
                memory="12g",
            )
            owned.environment["PAPER_LANCE_URL"] = "http://lance:8080"
        mounts = [
            f"type=bind,src={data.resolve()},dst=/corpus,readonly",
            f"type=bind,src={owned.output.resolve()},dst=/output",
        ]
        worker = owned.client(
            "workload",
            [
                "/driver/retrieval_workload.py",
                "--engine",
                engine,
                "--data",
                "/corpus",
                "--out",
                "/output/observations.json",
                *(["--wiring"] if wiring else []),
            ],
            env=[
                key
                for key in (
                    "PAPER_DB_URL",
                    "PAPER_ANTFLY_URL",
                    "PAPER_LANCE_URL",
                    "PAPER_SUBJECT_TAG",
                    "PAPER_ANTFLY_PROFILE",
                    "PAPER_EXACT_CONTROL_QUERIES",
                    "PAPER_PROTOCOL",
                )
                if key in owned.environment
            ],
            mounts=mounts,
            cpus=1,
            memory="1g",
        )
        if owned.wait_exit(worker, 21600) != 0:
            raise RuntimeError(
                "workload container failed; inspect workload.log in the attempt directory"
            )
        result = json.loads((owned.output / "observations.json").read_text())
        write_records(
            owned.output,
            result,
            {
                "client_image": get_client_image(),
                "instrument_sha256": {
                    path.name: sha(path) for path in (CAMPAIGN / "scripts").glob("*.py")
                },
                "requirements_sha256": sha(CAMPAIGN / "requirements.lock"),
                "protocol": result.get("protocol"),
                "antfly_subject": SUBJECT_TAG,
                "antfly_profile": ANTFLY_PROFILE,
                "postgres_build_config": {
                    "maintenance_work_mem": "6GB",
                    "shared_memory_capacity": "8g",
                }
                if engine.startswith("pgvector") or engine == "paradedb"
                else None,
                "prepared_manifest_sha256": sha(data / "manifest.json"),
                "resource_limits": {
                    "database": {"cpus": 4, "memory": "12g"},
                    "native_client": {"cpus": 1, "memory": "1g"},
                },
                "timing_qualification": "matched engine/client caps; remote persistent native query API, LanceDB through an explicit private SDK facade; quality qualification and three lifecycles required before peer ranking",
                "server_image": PGVECTOR_IMAGE
                if engine.startswith("pgvector")
                else ANTFLY_RUNTIME
                if engine == "antfly"
                else POSTGRES_IMAGE
                if engine == "paradedb"
                else None,
                "antfly_binary_sha256": ANTFLY_SHA
                if engine == "antfly" and SUBJECT_TAG != "v0.3.0"
                else None,
                "antfly_packaging": "official-release-image"
                if engine == "antfly" and SUBJECT_TAG == "v0.3.0"
                else None,
            },
        )
        (owned.output / "observations.json").unlink()
        # Expected ANN quality misses are outcomes; request errors, leaks and
        # failed exact controls retain stopped database state for diagnosis.
        success = not result.get("query_error_types") and all(
            c.get("exact_control", {}).get("recall_at_10", 0) >= 0.95
            and not c.get("holdout", {}).get("unauthorized", 0)
            for c in result["cells"].values()
        )
        print(
            json.dumps(
                {
                    "engine": engine,
                    "output": str(output),
                    "status": result["status"],
                    "cells": {
                        name: cell["status"] for name, cell in result["cells"].items()
                    },
                }
            ),
            flush=True,
        )
    except Exception as exc:
        if not (owned.output / "result.json").exists():
            write_records(
                owned.output,
                {
                    "status": "execution-incomplete",
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                },
                {"engine": engine, "client_image": get_client_image()},
            )
        raise
    finally:
        owned.close(success)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--engines", nargs="+", choices=ENGINES, default=list(ENGINES))
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--lifecycles", type=int, choices=(1, 3), default=1)
    parser.add_argument("--wiring", action="store_true")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    failures = []
    try:
        for ordinal in range(args.lifecycles):
            rotation = ordinal % len(args.engines)
            for engine in args.engines[rotation:] + args.engines[:rotation]:
                try:
                    lifecycle(
                        engine,
                        args.data,
                        args.out / f"{ordinal + 1:02d}-{engine}",
                        args.wiring,
                    )
                except OSError:
                    raise
                except Exception as exc:  # noqa: BLE001 - retain failed lifecycle and continue
                    failures.append(
                        {
                            "lifecycle": ordinal + 1,
                            "engine": engine,
                            "error_type": type(exc).__name__,
                        }
                    )
                    print(
                        json.dumps(
                            {"status": "attempt-failed-retained", **failures[-1]}
                        ),
                        flush=True,
                    )
    finally:
        fatal = sys.exc_info()[0]
        with (args.out / "terminal.json").open("x") as stream:
            json.dump(
                {
                    "status": "aborted"
                    if fatal
                    else "completed-with-failures"
                    if failures
                    else "completed",
                    "fatal_error_type": fatal.__name__ if fatal else None,
                    "execution_failures": failures,
                    "lifecycles": args.lifecycles,
                    "engines": args.engines,
                },
                stream,
                indent=2,
            )
        if (
            not args.wiring
            and args.lifecycles == 3
            and set(args.engines) == set(ENGINES)
        ):
            subprocess.run(
                [
                    sys.executable,
                    str(CAMPAIGN / "analysis/summarize_full.py"),
                    "--root",
                    str(args.out),
                    "--out",
                    str(args.out / "summary.json"),
                ],
                check=True,
            )
    raise SystemExit(bool(failures))
