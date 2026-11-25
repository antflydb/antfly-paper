"""Run a frozen real-OCR sample or full publisher replay on one benchmark host."""

import argparse
import fcntl
import json
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from urllib.request import urlopen

from native_documents import publish
from pdf_archive_campaign import (
    archive_binary_digest,
    port,
    stop,
    verify_representation_manifest,
)

HERE = Path(__file__).resolve().parent


@contextmanager
def service(log, command):
    with log.open("ab") as stream:
        proc = subprocess.Popen(command, stdout=stream, stderr=stream)
        try:
            yield proc
        finally:
            stop(proc)


def ready(proc, predicate):
    deadline = time.monotonic() + 7200
    while proc.poll() is None and time.monotonic() < deadline:
        try:
            if predicate():
                return
        except OSError:
            pass
        time.sleep(1)
    raise RuntimeError("service failed readiness")


def model_service_command(binary, models_dir, endpoint, out):
    config = out / "model-transport.json"
    publish(config, {"inference": {"http_max_body_mb": 64}})
    return [
        binary,
        "inference",
        "run",
        "--host",
        "127.0.0.1",
        "--port",
        str(endpoint),
        "--config",
        config,
        "--scratch-budget-mb",
        "16384",
        "--models-dir",
        models_dir,
    ]


def run(a):
    if a.embedding_cache and a.stage != "prepare":
        raise ValueError("--embedding-cache applies only to offline preparation")
    digest = archive_binary_digest(a.binary)
    native = a.stage == "native"
    plan = a.prepared_inputs or a.out / ("native-plan" if native else "inputs")
    workload = a.sample_workload or a.workload
    replay = [] if a.sample_workload else ["--replay-catalog", a.catalog]
    py = sys.executable
    prepared = plan / ("native-plan.json" if native else "manifest.json")
    if a.prepared_inputs and (a.stage == "prepare" or not prepared.is_file()):
        raise ValueError(
            "--prepared-inputs requires a completed plan for matched or native"
        )
    if prepared.exists():
        verify_representation_manifest(json.loads(prepared.read_text()), digest)
    with ExitStack() as stack:
        url = None
        needs_preparation = not prepared.exists() or bool(a.embedding_cache)
        if needs_preparation or a.stage == "matched":
            endpoint = port()
            url = f"http://127.0.0.1:{endpoint}"
            model = stack.enter_context(
                service(
                    a.out / "models.log",
                    model_service_command(a.binary, a.models_dir, endpoint, a.out),
                )
            )

            def model_ready():
                with urlopen(url + "/ai/v1/models", timeout=2) as r:
                    return r.status == 200

            ready(model, model_ready)
            options = ["--native-plan"] if native else []
            if not native and replay:
                rp = port()
                remote = stack.enter_context(
                    service(
                        a.out / "reader.log",
                        [
                            py,
                            HERE / "publisher_replay.py",
                            "serve",
                            "--application",
                            "--port",
                            str(rp),
                            "--catalog",
                            a.catalog,
                        ],
                    )
                )
                app_url = f"http://127.0.0.1:{rp}"

                def app_ready():
                    with urlopen(app_url + "/v1/status", timeout=2) as response:
                        return response.status == 200

                ready(remote, app_ready)
                replay += ["--application-reader", app_url]
            if not native:
                refs = a.out / "source-refs.json"
                # Publish only after the freshly started server verifies the sources.
                fresh = a.out / f"refs-{time.time_ns()}.json"
                source = stack.enter_context(
                    service(
                        a.out / "objects.log",
                        [
                            py,
                            HERE / "local_objects.py",
                            "--data",
                            a.data,
                            "--manifest",
                            fresh,
                            "--port",
                            str(port()),
                        ],
                    )
                )
                ready(source, fresh.exists)
                fresh.replace(refs)
                options += ["--refs", refs]
            if needs_preparation:
                subprocess.run(
                    [
                        py,
                        "-u",
                        HERE / "pdf_archive_inputs.py",
                        "--workload",
                        workload,
                        "--data",
                        a.data,
                        "--models",
                        url,
                        "--subject",
                        digest,
                        "--out",
                        plan,
                        *(
                            ["--embedding-cache", a.embedding_cache]
                            if a.embedding_cache
                            else []
                        ),
                        *options,
                        *replay,
                    ],
                    check=True,
                )
        if not native and a.sample_workload:
            from ocr_cost import summarize

            publish(a.out / "cost-estimate.json", summarize(a.data, workload, plan))
        if a.stage == "prepare":
            return
        if native:
            stack.close()
        subjects = ["antfly-native"] if native else a.engines
        subprocess.run(
            [
                py,
                "-u",
                HERE / "pdf_archive_campaign.py",
                "--inputs",
                plan,
                "--out",
                a.out / ("native-results" if native else "matched-results"),
                "--state",
                a.state,
                "--binary",
                a.binary,
                "--models-dir",
                a.models_dir,
                "--data",
                a.data,
                "--workload",
                workload,
                "--probe",
                "1" if a.sample_workload else "284",
                "--engines",
                *subjects,
                *(
                    ["--native-plan", *replay]
                    if native
                    else ["--pdf-models", url, "--pdf-refs", refs, *replay]
                ),
            ],
            check=True,
        )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("stage", choices=["prepare", "native", "matched"])
    for key in ("data", "catalog", "workload", "state", "out", "models-dir", "binary"):
        p.add_argument("--" + key, type=Path, required=True)
    p.add_argument(
        "--prepared-inputs",
        type=Path,
        help="reuse a completed input directory with a fresh output cohort",
    )
    p.add_argument(
        "--embedding-cache",
        type=Path,
        help="SQLite path for preparation across corpus revisions",
    )
    p.add_argument("--sample-workload", type=Path)
    p.add_argument(
        "--engines",
        nargs="+",
        choices=["antfly", "lance", "postgres", "opensearch", "vespa"],
        default=["antfly", "lance", "postgres", "opensearch", "vespa"],
    )
    a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    with (a.out / ".study.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run(a)
