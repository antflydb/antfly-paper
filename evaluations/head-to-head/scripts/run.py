"""Owned, bounded document application qualification and backfill experiments."""

import argparse
import concurrent.futures
import json
import subprocess
import sys
import time
from pathlib import Path

from adapters import Antfly, Postgres, wait
from common import (
    DETECTOR,
    DETECTOR_DIRECTORY,
    EMBEDDER,
    MIGRATION_EMBEDDER,
    READER,
    embed,
    findings,
    request,
)
from traffic import drive, summarize
from vector_oracle import negative_controls
from vector_oracle import validate_space as validate_antfly_space

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.append(str(ROOT / "evaluations/retrieval/scripts"))
import runtime  # noqa: E402

APPLICATION_PROTOCOL = "document-main-v3" if runtime.CANDIDATE else "document-v1"
runtime.LABEL = "antfly-paper.application=" + APPLICATION_PROTOCOL


def run(args):
    if not 1 <= args.copies <= 1024:
        raise ValueError("copies must be in 1..1024")
    if args.engine != "antfly" and args.antfly_reader_auth not in (None, "basic"):
        raise ValueError("--antfly-reader-auth applies only to Antfly")
    out = args.out.resolve()
    owned = runtime.Owned(out)
    out.mkdir(parents=True, exist_ok=False)
    runtime.create_network(owned.network, internal=False)
    instrument_paths = [
        *HERE.glob("*.py"),
        Path(runtime.__file__),
        runtime.SUBJECT_IMPLEMENTATION,
        ROOT / "evaluations/retrieval/requirements.lock",
        ROOT / "evaluations/retrieval/Dockerfile",
    ]
    source = {str(p.relative_to(ROOT)): runtime.sha(p) for p in instrument_paths}
    checks = []
    index_creations = []
    phases = []
    success = False
    phase = "setup"

    reader_auth = "basic"
    if args.engine == "antfly":
        reader_auth = args.antfly_reader_auth or "api_key"

    def check(name, passed, **details):
        row = {"name": name, "passed": bool(passed), **details}
        checks.append(row)
        print(json.dumps(row), flush=True)
        if not passed:
            raise AssertionError(name)

    def healthy(url):
        try:
            return request(url)
        except (OSError, ValueError):
            return False

    try:
        mounts = []
        weights = {}
        for model in (EMBEDDER, MIGRATION_EMBEDDER, READER, DETECTOR):
            directory = (
                args.migration_models if model == MIGRATION_EMBEDDER else args.models
            ) / (DETECTOR_DIRECTORY if model == DETECTOR else model)
            if not directory.is_dir():
                raise ValueError("missing model directory: " + model)
            mounts.append(
                f"type=bind,src={directory.resolve()},dst=/models/{model},readonly"
            )
            for path in directory.rglob("*"):
                if path.is_file() and not path.name.endswith(".antfly-sha256-cache"):
                    weights[model + "/" + str(path.relative_to(directory))] = (
                        runtime.sha(path)
                    )
        (out / "model-files.json").write_text(json.dumps(weights, indent=2) + "\n")
        native = owned.container(
            "native",
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
                "--host-budget-mb",
                "7168",
                "--combined-budget-mb",
                "7168",
                "--scratch-budget-mb",
                "3072",
            ],
            mounts=mounts,
            ports=[8090],
            cpus=2,
            memory="8g",
        )
        native_url = f"http://127.0.0.1:{owned.port(native, 8090)}"
        wait(lambda: healthy(native_url + "/ai/v1/models"))
        owned.environment["PAPER_NATIVE_URL"] = "http://native:8090"
        gateway = owned.client(
            "models",
            ["/application/gateway.py"],
            env=["PAPER_NATIVE_URL"],
            mounts=[f"type=bind,src={HERE},dst=/application,readonly"],
            ports=[8090],
            cpus=0.25,
            memory="256m",
        )
        models = f"http://127.0.0.1:{owned.port(gateway, 8090)}"
        wait(lambda: healthy(models + "/health"))

        def control(hold, allow_input_sha256=()):
            return request(
                models + "/control",
                {"hold": hold, "allow_input_sha256": sorted(allow_input_sha256)},
                headers={"Authorization": "Bearer " + owned.token},
            )

        def audit():
            return request(
                models + "/audit", headers={"Authorization": "Bearer " + owned.token}
            )["calls"]

        phase = "pdf-extraction"
        if args.extracted:
            (out / "extracted.json").write_bytes(args.extracted.read_bytes())
        else:
            frontend = owned.client(
                "frontend",
                [
                    "/application/frontend.py",
                    "--data",
                    "/corpus",
                    "--out",
                    "/output/extracted.json",
                ],
                mounts=[
                    f"type=bind,src={HERE},dst=/application,readonly",
                    f"type=bind,src={args.data.resolve()},dst=/corpus,readonly",
                    f"type=bind,src={out},dst=/output",
                ],
                cpus=1,
                memory="1g",
            )
            if owned.wait_exit(frontend, timeout=600) != 0:
                raise RuntimeError("PDF frontend failed")
        rows = json.loads((out / "extracted.json").read_text())

        def correction(version):
            job = owned.client(
                "correction" + str(version),
                [
                    "/application/frontend.py",
                    "--data",
                    "/corpus",
                    "--correction-version",
                    str(version),
                    "--out",
                    f"/output/correction-{version}.json",
                ],
                mounts=[
                    f"type=bind,src={HERE},dst=/application,readonly",
                    f"type=bind,src={args.data.resolve()},dst=/corpus,readonly",
                    f"type=bind,src={out},dst=/output",
                ],
                cpus=1,
                memory="1g",
            )
            if owned.wait_exit(job, timeout=180) != 0:
                raise RuntimeError("replacement PDF extraction failed")
            row = json.loads((out / f"correction-{version}.json").read_text())[0]
            if row["id"] != "d000" or row["version"] != version:
                raise ValueError("wrong correction identity")
            return {
                k: row[k]
                for k in ("body", "tenant", "version", "page", "source_pdf_sha256")
            }

        annotations = json.loads((args.data / "annotations.json").read_text())[
            "documents"
        ]
        tenant_by_id = {row["id"]: row["tenant"] for row in annotations}
        check(
            "native-and-scanned-routing",
            len(rows) == 8 and sum(r["extraction"] == "ocr" for r in rows) == 4,
        )
        # Ground truth is authored source text, never the OCR model's answer.
        import difflib

        accuracy = []
        for row, truth in zip(rows, annotations):
            accuracy.append(
                {
                    "id": row["id"],
                    "character_similarity": difflib.SequenceMatcher(
                        None,
                        " ".join(truth["body"].split()),
                        " ".join(row["body"].split()),
                    ).ratio(),
                    "email_preserved": truth["email"] in row["body"],
                }
            )
        (out / "extraction-quality.json").write_text(
            json.dumps(accuracy, indent=2) + "\n"
        )
        check(
            "qualification-extracted-content",
            all(x["character_similarity"] >= 0.90 for x in accuracy),
            canonical_email_exact=sum(x["email_preserved"] for x in accuracy),
            documents=len(accuracy),
        )
        # Qualify/warm the exact span-bearing model request before enrollment.
        detector_control = findings(rows[0]["body"], models)
        check(
            "real-detector-span-control",
            bool(detector_control["data"][0].get("entities")),
        )
        phase = "database-start"
        validation_inputs = set()
        if args.engine == "antfly":
            owned.environment["ANTFLY_BOOTSTRAP_ADMIN_PASSWORD"] = owned.password
            config = out / "config.json"
            config.write_text(
                json.dumps({"inference": {"api_url": "http://models:8090"}})
            )
            volume = owned.volume("database")
            service = owned.container(
                "antfly",
                runtime.ANTFLY_IMAGE,
                [
                    "standalone",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "8080",
                    "--auth",
                    "true",
                    "--health",
                    "false",
                    "--data-dir",
                    "/antflydb",
                    "--config",
                    "/config.json",
                ],
                env=["ANTFLY_BOOTSTRAP_ADMIN_PASSWORD"],
                mounts=[
                    f"type=volume,src={volume},dst=/antflydb",
                    f"type=bind,src={config},dst=/config.json,readonly",
                ],
                ports=[8080],
                cpus=3,
                memory="6g",
                ulimits=["nofile=65536:524288"],
            )
            url = f"http://127.0.0.1:{owned.port(service, 8080)}"
            import base64

            headers = {
                "Authorization": "Basic "
                + base64.b64encode(("admin:" + owned.password).encode()).decode()
            }

            def ant_ready():
                try:
                    return isinstance(
                        request(url + "/db/v1/tables", headers=headers), list
                    )
                except (OSError, ValueError):
                    return False

            wait(ant_ready)
            validation_begin = len(audit())
            app = Antfly(
                url,
                owned.password,
                reader_auth=reader_auth,
                index_creations=index_creations,
            )
            # No source rows exist yet: these successful embedding calls are
            # admission probes, learned from the subject rather than its text.
            validation_inputs = {
                row["input_sha256"]
                for row in audit()[validation_begin:]
                if row["task"] == "embed" and row.get("status") == 200
            }
            if not validation_inputs or None in validation_inputs:
                raise RuntimeError("initial index admission probe was not observed")
        else:
            volume = owned.volume("database")
            database = owned.container(
                "postgres",
                runtime.PGVECTOR_IMAGE,
                ["postgres", "-c", "shared_buffers=1GB"],
                env=["POSTGRES_PASSWORD"],
                mounts=[f"type=volume,src={volume},dst=/var/lib/postgresql"],
                cpus=2,
                memory="5g",
                ulimits=["nofile=65536:524288"],
            )

            def pg_ready():
                try:
                    return owned.sql(database, "SELECT 1").strip() == "1"
                except subprocess.CalledProcessError:
                    return False

            wait(pg_ready)
            owned.environment["PAPER_DB_URL"] = (
                f"postgresql://postgres:{owned.password}@postgres:5432/postgres"
            )
            owned.environment["PAPER_WORKER_BATCH"] = "32" if runtime.CANDIDATE else "1"
            service = owned.client(
                "pipeline",
                ["/application/pipeline.py"],
                env=["PAPER_DB_URL", "PAPER_WORKER_BATCH"],
                mounts=[f"type=bind,src={HERE},dst=/application,readonly"],
                ports=[8091],
                cpus=1,
                memory="1g",
                ulimits=["nofile=65536:524288"],
            )
            url = f"http://127.0.0.1:{owned.port(service, 8091)}"

            def pipe_ready():
                try:
                    return request(
                        url + "/ready",
                        {},
                        headers={"Authorization": "Bearer " + owned.token},
                    )
                except (OSError, ValueError):
                    return False

            wait(pipe_ready)
            app = Postgres(url, owned.token, owned.password)
        phase = "initial-ingest"
        protected_begin = len(audit())
        if args.privacy_before_write:
            app.privacy()
        for row in rows:
            app.write(
                row["id"],
                {k: row[k] for k in ("body", "tenant", "version", "page")},
                sync=args.initial_sync,
            )
        wait(app.ready)
        vectors = embed([r["query"] for r in annotations], models)

        def assess(protected=False, name="a0"):
            hits = []
            for truth, vector in zip(annotations, vectors):
                result = app.query(truth["tenant"], vector, name)
                if not result["available"]:
                    return False
                if any(
                    tenant_by_id.get(h["id"]) != truth["tenant"] for h in result["hits"]
                ):
                    return False
                if not result["hits"] or result["hits"][0]["id"] != truth["id"]:
                    return False
                if protected and any("@" in h.get("body", "") for h in result["hits"]):
                    return False
                hits.extend(result["hits"])
            return bool(hits)

        check("permissioned-semantic-top1", assess(args.privacy_before_write))
        phase = "privacy-activation"
        before = protected_begin if args.privacy_before_write else len(audit())
        if not args.privacy_before_write:
            app.privacy()
        wait(lambda: assess(True))
        check("protected-text-and-relevance", assess(True))
        calls = audit()[before:]
        embeds = [x for x in calls if x["task"] == "embed"]
        check(
            "protected-embedding-model-boundary",
            bool(embeds) and not any(x["original_email_present"] for x in embeds),
            embedding_calls=len(embeds),
        )
        check(
            "protected-fulltext",
            all(
                not app.query(t, vectors[0], text="example.invalid")["hits"]
                for t in ("alpha", "beta")
            ),
        )
        phase = "restart-after-publication"
        before = len(audit())
        recovery_began = time.monotonic()
        runtime.docker(["kill", service])
        runtime.docker(["start", service])
        if args.engine == "antfly":
            app.url = f"http://127.0.0.1:{owned.port(service, 8080)}"
        else:
            app.url = f"http://127.0.0.1:{owned.port(service, 8091)}"

        def restarted():
            try:
                return assess(True)
            except (OSError, ValueError):
                return False

        wait(restarted)
        check(
            "committed-artifacts-survive-restart",
            assess(True),
            model_calls_after_restart=len(audit()) - before,
            recovery_seconds=time.monotonic() - recovery_began,
            injected_actions=[
                "kill application process",
                "restart same durable volume",
            ],
        )
        phase = "held-update-and-delete"
        control(True)
        changed = {
            "body": "Orchard maintenance now requires pruning apple trees in late winter, before spring growth.",
            "tenant": "alpha",
            "version": 2,
            "page": 1,
        }
        if runtime.CANDIDATE:
            changed = correction(2)
        app.write("d000", changed)
        app.write(
            "temporary",
            {
                "body": "Temporary battery storage guidance.",
                "tenant": "alpha",
                "version": 1,
                "page": 1,
            },
        )
        wait(lambda: any(x["pending"] and x["task"] == "embed" for x in audit()))
        third = (
            correction(3)
            if runtime.CANDIDATE
            else {
                **changed,
                "body": "Prune apple trees in late winter. Corrected orchard guidance.",
                "version": 3,
            }
        )
        app.write("d000", third)
        app.delete("temporary", sync="write" if runtime.CANDIDATE else "full_index")
        recovery_began = time.monotonic()
        runtime.docker(["kill", service])
        runtime.docker(["start", service])
        app.url = f"http://127.0.0.1:{owned.port(service, 8080 if args.engine == 'antfly' else 8091)}"
        control(False)

        def recovered_ready():
            try:
                return app.ready()
            except (OSError, ValueError):
                return False

        wait(recovered_ready)
        observed = app.query("alpha", vectors[0])
        check(
            "held-update-converges-and-delete-persists",
            observed["available"]
            and not any(h["id"] == "temporary" for h in observed["hits"])
            and any(h["id"] == "d000" and h["version"] == 3 for h in observed["hits"]),
            recovery_seconds=time.monotonic() - recovery_began,
            injected_actions=[
                "kill application process",
                "restart same durable volume",
                "release held provider responses",
            ],
        )
        phase = "permission-restriction"
        app.write(
            "d000",
            {
                **third,
                "version": 4,
                "tenant": "beta",
            },
            sync="full_index",
        )
        check(
            "permission-restriction",
            not any(h["id"] == "d000" for h in app.query("alpha", vectors[0])["hits"])
            and any(h["id"] == "d000" for h in app.query("beta", vectors[0])["hits"]),
        )
        phase = "model-migration-during-writes"
        migration_begin = len(audit())
        control(True, validation_inputs)
        app.generation("b1", MIGRATION_EMBEDDER)
        wait(
            lambda: any(
                row["pending"] and row["hold_applied"]
                for row in audit()[migration_begin:]
            )
        )
        fifth = (
            correction(5)
            if runtime.CANDIDATE
            else {
                **changed,
                "body": "Prune apple trees in late winter. Final orchard guidance.",
                "version": 5,
                "tenant": "beta",
            }
        )
        app.write("d000", fifth)
        runtime.docker(["kill", service])
        runtime.docker(["start", service])
        app.url = f"http://127.0.0.1:{owned.port(service, 8080 if args.engine == 'antfly' else 8091)}"
        control(False)

        def migration_ready():
            try:
                return app.ready("b1") and app.ready("a0")
            except (OSError, ValueError):
                return False

        wait(migration_ready)
        b_queries = embed([r["query"] for r in annotations], models, MIGRATION_EMBEDDER)

        vector_receipts = []
        source_ledger = {
            row["id"]: {
                "tenant": tenant_by_id[row["id"]],
                "version": row["version"],
                "page": row["page"],
            }
            for row in rows
        }
        source_ledger["d000"] = {
            field: fifth[field] for field in ("tenant", "version", "page")
        }

        def validate_space(name, model, queries):
            if args.engine == "antfly" and runtime.CANDIDATE:
                try:
                    return validate_antfly_space(
                        app,
                        queries,
                        name,
                        model,
                        source_ledger,
                        lambda text: embed([text], models, model)[0],
                        vector_receipts,
                    )
                finally:
                    (out / "vector-inspections.json").write_text(
                        json.dumps(vector_receipts, indent=2) + "\n"
                    )
            scored = 0
            for tenant in ("alpha", "beta"):
                result = app.query(tenant, queries[0], name, include_vectors=True)
                if not result["available"] or not result["hits"]:
                    return False
                for hit in result["hits"]:
                    expected = embed([hit["body"]], models, model)[0]
                    observed = hit.get("_embeddings", {}).get(name, [])
                    if (
                        len(observed) != len(expected)
                        or sum((a - b) ** 2 for a, b in zip(observed, expected)) > 1e-5
                    ):
                        return False
                    scored += 1
            return scored == 8

        check(
            "migration-new-space-vectors",
            validate_space("b1", MIGRATION_EMBEDDER, b_queries),
        )
        if args.engine == "antfly" and runtime.CANDIDATE:
            accepted = vector_receipts[0]["snapshot"]
            controls = negative_controls(
                accepted,
                source_ledger[accepted["document_id"]],
                lambda text: embed([text], models, MIGRATION_EMBEDDER)[0],
            )
            check(
                "vector-oracle-negative-controls",
                all(controls.values()),
                controls=controls,
            )
        check(
            "migration-old-space-still-consistent",
            validate_space("a0", EMBEDDER, vectors),
        )
        app.generation("a2", EMBEDDER)
        wait(lambda: app.ready("a2"))
        check("rollback-new-a-generation", validate_space("a2", EMBEDDER, vectors))
        if args.performance:
            phase = "scaled-controlled-ingest"
            app.drop_generation("a0")
            app.drop_generation("b1")
            ledger = {
                r["id"]: {"tenant": r["tenant"], "topic": r["topic"]}
                for r in annotations
            }
            ledger["d000"]["tenant"] = "beta"
            for copy in range(1, args.copies):
                for row, truth in zip(rows, annotations):
                    identity = row["id"] + "c" + str(copy)
                    app.write(
                        identity,
                        {k: row[k] for k in ("body", "tenant", "version", "page")},
                    )
                    ledger[identity] = {
                        "tenant": truth["tenant"],
                        "topic": truth["topic"],
                    }
            wait(lambda: app.ready("a2"), 600)
            if runtime.CANDIDATE:
                scaled = app.completion("a2")
                check(
                    "scaled-ingest-complete-counts",
                    scaled.get("observation_complete") is True
                    and scaled.get("source_total")
                    == scaled.get("produced")
                    == len(ledger),
                    coverage=scaled,
                )
            phase = "serving-only"
            serving = drive(
                app,
                annotations,
                lambda text: embed([text], models)[0],
                generation="a2",
                duration=args.duration,
                rate=args.rate,
                ledger=ledger,
            )
            phases.append({"name": phase, "summary": summarize(serving)})
            (out / "serving-only.json").write_text(json.dumps(serving, indent=2) + "\n")
            phase = "serving-with-backfill"
            began = time.monotonic()
            app.generation("b2", MIGRATION_EMBEDDER)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:

                def observe_completion():
                    wait(lambda: app.ready("b2"), 600)
                    return time.monotonic() - began

                completion = pool.submit(observe_completion)
                concurrent_rows = drive(
                    app,
                    annotations,
                    lambda text: embed([text], models)[0],
                    generation="a2",
                    duration=args.duration,
                    rate=args.rate,
                    ledger=ledger,
                )
                backfill_seconds = completion.result()
            completed_seconds = time.monotonic() - began
            completed_count = len(ledger)
            if runtime.CANDIDATE:
                backfilled = app.completion("b2")
                check(
                    "backfill-complete-counts",
                    backfilled.get("observation_complete") is True
                    and backfilled.get("source_total")
                    == backfilled.get("produced")
                    == len(ledger),
                    coverage=backfilled,
                )
                completed_count = backfilled["produced"]
            (out / "serving-with-backfill.json").write_text(
                json.dumps(concurrent_rows, indent=2) + "\n"
            )
            phases.append(
                {
                    "name": phase,
                    "summary": summarize(concurrent_rows),
                    "completed_target_documents": completed_count,
                    "window_and_completion_seconds": completed_seconds,
                    "backfill_completion_seconds": backfill_seconds,
                    "offered_qps": args.rate,
                    "window_seconds": args.duration,
                    "model_placement": "isolated shared remote service in both architectures; equal service caps",
                }
            )
            check(
                "fixed-load-protected-output",
                all(
                    not r.get("email_like_output") and not r.get("unauthorized", 0)
                    for r in serving + concurrent_rows
                ),
            )
            check("backfill-completed", app.ready("b2"), documents=len(ledger))
        success = True
    except Exception as exc:
        (out / "failure.json").write_text(
            json.dumps(
                {
                    "phase": phase,
                    "error": type(exc).__name__,
                    "message": str(exc)[:500],
                },
                indent=2,
            )
            + "\n"
        )
        raise
    finally:
        current = {str(p.relative_to(ROOT)): runtime.sha(p) for p in instrument_paths}
        (out / "result.json").write_text(
            json.dumps(
                {
                    "protocol": APPLICATION_PROTOCOL,
                    "subject": runtime.subject_provenance(),
                    "engine": args.engine,
                    "status": "completed" if success else "execution-incomplete",
                    "fresh_pdf_frontend": not bool(args.extracted),
                    "privacy_before_write": args.privacy_before_write,
                    "initial_sync": args.initial_sync,
                    "reader_auth": reader_auth,
                    **(
                        {"index_creations": index_creations}
                        if args.engine == "antfly"
                        else {}
                    ),
                    "checks": checks,
                    "phases": phases,
                    "source_hashes": source,
                    "source_unchanged": source == current,
                    "not_run": [
                        "natural document corpus generalization",
                        "on-disk privacy audit",
                        "co-located inference comparison",
                        "exact response-before-commit internal failpoint",
                    ]
                    + (
                        [] if args.performance else ["fixed-load backfill interference"]
                    ),
                },
                indent=2,
            )
            + "\n"
        )
        try:
            calls = audit() if "models" in locals() else []
            for offset in range(0, max(1, len(calls)), 2000):
                raw = json.dumps(calls[offset : offset + 2000], indent=2) + "\n"
                if len(raw.encode()) >= 1048576:
                    raise ValueError("model audit record exceeds budget")
                (out / f"model-audit-{offset // 2000:03d}.json").write_text(raw)
        except Exception as exc:
            (out / "audit-error.json").write_text(
                json.dumps({"error": type(exc).__name__}) + "\n"
            )
        (out / "provenance.json").write_text(
            json.dumps(
                {
                    "protocol": APPLICATION_PROTOCOL,
                    "subject": runtime.subject_provenance(),
                    "subject_image": runtime.ANTFLY_IMAGE,
                    "client_image": runtime.get_client_image(),
                    "postgres_image": runtime.PGVECTOR_IMAGE,
                    "fixture_annotations_sha256": runtime.sha(
                        args.data / "annotations.json"
                    ),
                    "model_files_sha256": runtime.sha(out / "model-files.json")
                    if (out / "model-files.json").exists()
                    else None,
                    "caps": {
                        "antfly_database": {"cpus": 3, "memory_gib": 6},
                        "postgres_database": {"cpus": 2, "memory_gib": 5},
                        "postgres_application": {"cpus": 1, "memory_gib": 1},
                        "common_models": {"cpus": 2, "memory_gib": 8},
                        "common_gateway": {"cpus": 0.25, "memory_mib": 256},
                        "common_frontend": {"cpus": 1, "memory_gib": 1},
                    },
                    "arguments": {
                        "performance": args.performance,
                        "copies": args.copies,
                        "duration": args.duration,
                        "rate": args.rate,
                        "diagnostic_extraction_reuse": bool(args.extracted),
                        "privacy_before_write": args.privacy_before_write,
                        "initial_sync": args.initial_sync,
                        "reader_auth": reader_auth,
                        "postgres_worker_batch": 32 if runtime.CANDIDATE else 1,
                    },
                },
                indent=2,
            )
            + "\n"
        )
        owned.close(success=success)
        (out / "receipt.json").write_text(
            json.dumps(
                {
                    "sha256": {
                        p.name: runtime.sha(p)
                        for p in out.iterdir()
                        if p.is_file() and p.name != "receipt.json"
                    }
                },
                indent=2,
            )
            + "\n"
        )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--engine", choices=["antfly", "postgres"], required=True)
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--models", type=Path, required=True)
    p.add_argument(
        "--migration-models", type=Path, default=ROOT / ".data/document-models"
    )
    p.add_argument("--out", type=Path, required=True)
    p.add_argument(
        "--extracted",
        type=Path,
        help="diagnostic reuse of previously extracted controlled fixture; not a fresh end-to-end lifecycle",
    )
    p.add_argument("--performance", action="store_true")
    p.add_argument(
        "--antfly-reader-auth",
        choices=["basic", "api_key"],
        help="Antfly tenant read credentials (default: api_key); administration uses the bootstrap login",
    )
    p.add_argument("--initial-sync", choices=["write", "full_index"], default="write")
    p.add_argument(
        "--privacy-before-write",
        action="store_true",
        help="separate protective-from-first-write condition; not policy activation over existing rows",
    )
    p.add_argument("--copies", type=int, default=128)
    p.add_argument("--duration", type=float, default=60)
    p.add_argument("--rate", type=float, default=2)
    run(p.parse_args())
