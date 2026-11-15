"""Native Antfly controls for pinned identities, recorded replay and privacy.

Synthetic correctness only; this is not a peer ranking or full storage audit.
"""

import argparse
import concurrent.futures
import hashlib
import importlib.util
import json
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "evaluations/lifecycle/scripts"))
# A distinct module name avoids importing this entrypoint as the shared runner.
spec = importlib.util.spec_from_file_location(
    "lifecycle_core", ROOT / "evaluations/lifecycle/scripts/run.py"
)
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)
runtime, call, wait = core.runtime, core.call, core.wait
runtime.LABEL = "antfly-paper.lifecycle=extended-v2"
CANARY = "lifecycle-canary@example.invalid"


def run(out):
    owned = runtime.Owned(out)
    owned.output.mkdir(parents=True, exist_ok=False)
    runtime.create_network(owned.network, internal=False)
    checks = []
    phase = "setup"
    success = False
    paths = [
        *HERE.glob("*.py"),
        *core.HERE.glob("*.py"),
        Path(runtime.__file__),
        runtime.SUBJECT_IMPLEMENTATION,
    ]
    hashes = {str(p.relative_to(ROOT)): runtime.sha(p) for p in paths}

    def check(name, passed, **detail):
        checks.append({"name": name, "passed": bool(passed), **detail})
        print(json.dumps(checks[-1]), flush=True)
        if not passed:
            raise AssertionError(name)

    try:
        provider = owned.client(
            "provider-a",
            ["/extended/model.py"],
            env=["PAPER_TOKEN"],
            mounts=[
                f"type=bind,src={HERE},dst=/extended,readonly",
                f"type=bind,src={core.HERE},dst=/shared,readonly",
            ],
            ports=[8090],
            cpus=0.5,
            memory="256m",
        )
        url = f"http://127.0.0.1:{owned.port(provider, 8090)}"

        def audit():
            return call(url + "/control", token=owned.token)

        def ready():
            try:
                return audit()
            except OSError:
                return False

        wait(ready)

        def configure(**mode):
            return call(url + "/control", "POST", mode, token=owned.token)

        owned.environment["OPENAI_API_KEY"] = "synthetic-fixture"
        original = owned.container

        def container(*args, env=(), **kwargs):
            return original(*args, env=[*env, "OPENAI_API_KEY"], **kwargs)

        owned.container = container
        engine = core.Antfly(owned)
        phase = "committed-nondeterministic-replay"
        engine.api("/tables/decisions", "POST", {"num_shards": 1})
        engine.api(
            "/tables/decisions/artifacts/decision/enrichment",
            "PUT",
            {
                "kind": "asset",
                "field": "body",
                "content_type": "application/json",
                "producer": {
                    "type": "generator",
                    "config": {
                        "generator": {
                            "provider": "openai",
                            "model": "gpt-4.1",
                            "url": "http://provider-a:8090/v1",
                        },
                        "output": "json_schema",
                        "batch": {"max_items_per_call": 1},
                        "output_schema": {
                            "type": "object",
                            "properties": {"verdict": {"type": "string"}},
                            "required": ["verdict"],
                            "additionalProperties": False,
                        },
                    },
                },
            },
        )
        payload = {
            "inserts": {"doc": {"body": "same committed decision input"}},
            "sync_level": "enrichments",
        }
        engine.api("/tables/decisions/batch", "POST", payload, 40)

        def artifact():
            result = engine.api(
                "/tables/decisions/documents/doc?fields=_artifacts.decision"
            )
            value = result.get("_artifacts", {}).get("decision", {}).get("value")
            return json.loads(value) if isinstance(value, str) else value

        value = wait(artifact)
        before = len([x for x in audit()["calls"] if x["task"] == "decision"])
        check("recorded-decision-exists", bool(value.get("verdict")) and before > 0)
        configure(decision_fail=True)
        engine.restart()
        recovered = wait(artifact)
        after = len([x for x in audit()["calls"] if x["task"] == "decision"])
        check(
            phase,
            recovered == value and after == before,
            model_calls_before=before,
            model_calls_after=after,
        )
        phase = "identical-source-decision-reuse"
        engine.api("/tables/decisions/batch", "POST", payload, 40)
        check(
            phase,
            artifact() == value
            and len([x for x in audit()["calls"] if x["task"] == "decision"]) == before,
        )
        configure(decision_fail=False)
        phase = "pinned-provider-identity"
        engine.api("/tables/pinned", "POST", {"num_shards": 1})
        engine.api(
            "/tables/pinned/indexes/vec",
            "POST",
            {
                "type": "embeddings",
                "dimension": 2,
                "field": "body",
                "embedder": {
                    "provider": "antfly",
                    "model": "fixture",
                    "api_url": "http://provider-a:8090",
                    "model_identity": "a" * 64,
                },
            },
        )
        engine.api(
            "/tables/pinned/batch",
            "POST",
            {
                "inserts": {"good": {"body": "pinned good source"}},
                "sync_level": "full_index",
            },
            40,
        )

        def pinned():
            cell = engine.api(
                "/query",
                "POST",
                {
                    "table": "pinned",
                    "indexes": ["vec"],
                    "embeddings": {"vec": [1, 0]},
                    "fields": ["_embeddings"],
                    "limit": 10,
                },
            )["responses"][0]
            return cell

        check(
            "pinned-positive-control",
            len(pinned().get("hits", {}).get("hits", [])) == 1,
        )
        for name, mode in [
            ("wrong-revision-ack", {"revision": "B"}),
            ("missing-revision-ack", {"revision": "A", "ack": False}),
        ]:
            phase = name
            configure(**mode)
            start = len(audit()["calls"])
            engine.api(
                "/tables/pinned/batch",
                "POST",
                {
                    "inserts": {name: {"body": "must not publish " + name}},
                    "sync_level": "write",
                },
            )
            wait(
                lambda start=start, name=name: any(
                    hashlib.sha256(("must not publish " + name).encode()).hexdigest()
                    in c.get("inputs_sha256", [])
                    for c in audit()["calls"][start:]
                )
            )
            checked = 0
            rejected_hits = 0
            for _ in range(10):
                cell = pinned()
                hits = cell.get("hits", {}).get("hits", [])
                checked += 1
                rejected_hits += sum(h["_id"] == name for h in hits)
                time.sleep(0.02)
            complete = (
                engine.api("/tables/pinned/indexes/vec")
                .get("status", {})
                .get("coverage", {})
                .get("complete")
            )
            check(
                name,
                rejected_hits == 0 and checked > 0 and complete is False,
                queries=checked,
                index_complete=complete,
            )
            configure(revision="A", ack=True)
        fixture = __import__("fixture")

        def pending_for(text):
            state = audit()
            digest = hashlib.sha256(text.encode()).hexdigest()
            return [
                c["id"]
                for c in state["calls"]
                if c["id"] in state["pending"] and digest in c.get("inputs_sha256", [])
            ]

        def vector_hits(table):
            cell = engine.api(
                "/query",
                "POST",
                {
                    "table": table,
                    "indexes": ["vec"],
                    "embeddings": {"vec": [1, 0]},
                    "fields": ["_embeddings"],
                    "limit": 10,
                },
            )["responses"][0]
            if cell.get("status") != 200:
                raise RuntimeError("vector observation unavailable")
            return cell.get("hits", {}).get("hits", [])

        phase = "superseded-response-before-latest-publication"
        engine.api("/tables/supersession", "POST", {"num_shards": 1})
        engine.api(
            "/tables/supersession/indexes/vec",
            "POST",
            {
                "type": "embeddings",
                "dimension": 2,
                "field": "body",
                "embedder": {
                    "provider": "antfly",
                    "model": "fixture",
                    "api_url": "http://provider-a:8090",
                    "model_identity": "a" * 64,
                },
            },
        )
        engine.api(
            "/tables/supersession/batch",
            "POST",
            {
                "inserts": {"doc": {"body": "previous published source"}},
                "sync_level": "full_index",
            },
            40,
        )
        configure(embedding_hold=True)
        obsolete = "obsolete held source two"
        latest = "latest held source three"
        engine.api(
            "/tables/supersession/batch",
            "POST",
            {"inserts": {"doc": {"body": obsolete}}, "sync_level": "write"},
        )
        old_ids = wait(lambda: pending_for(obsolete))
        engine.api(
            "/tables/supersession/batch",
            "POST",
            {"inserts": {"doc": {"body": latest}}, "sync_level": "write"},
        )
        configure(release_ids=old_ids)
        wait(lambda: pending_for(latest))
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            observing = pool.submit(vector_hits, "supersession")
            time.sleep(0.25)
            before = observing.result() if observing.done() else None
            configure(embedding_hold=False, release_all=True)
            observed = observing.result(timeout=15)
        forbidden = sum(
            fixture.matches(h["_source"]["_embeddings"]["vec"], obsolete, "A")
            for h in observed
        )
        check(
            phase,
            forbidden == 0,
            latest_response_proven_held=True,
            query_blocked_until_release=before is None,
        )

        def latest_visible():
            hits = vector_hits("supersession")
            return len(hits) == 1 and fixture.matches(
                hits[0]["_source"]["_embeddings"]["vec"], latest, "A"
            )

        wait(latest_visible)
        phase = "provider-alias-aba-held-response"
        aba = "alias B reply released after returning to A"
        good = "alias restored A positive source"
        configure(revision="B", embedding_hold=True)
        engine.api(
            "/tables/pinned/batch",
            "POST",
            {"inserts": {"aba": {"body": aba}}, "sync_level": "write"},
        )
        held = wait(lambda: pending_for(aba))
        configure(revision="A", embedding_hold=False)
        engine.api(
            "/tables/pinned/batch",
            "POST",
            {"inserts": {"restored": {"body": good}}, "sync_level": "write"},
        )
        configure(release_ids=held)
        wrong = 0
        seen_good = False
        for _ in range(20):
            hits = vector_hits("pinned")
            wrong += sum(h["_source"]["_embeddings"]["vec"][0] < 0 for h in hits)
            seen_good = seen_good or any(
                h["_id"] == "restored"
                and fixture.matches(h["_source"]["_embeddings"]["vec"], good, "A")
                for h in hits
            )
            time.sleep(0.05)
        check(
            phase,
            wrong == 0 and seen_good,
            incompatible_space_hits=wrong,
            restored_model_positive_control=seen_good,
        )
        phase = "privacy-downstream-coverage"
        engine.api("/tables/privacy", "POST", {"num_shards": 1})
        engine.api(
            "/tables/privacy/indexes/vec",
            "POST",
            {
                "type": "embeddings",
                "dimension": 2,
                "field": "body",
                "embedder": {
                    "provider": "openai",
                    "model": "fixture",
                    "url": "http://provider-a:8090",
                    "api_key": "synthetic-fixture",
                },
            },
        )
        fields = {
            "category": {"type": "string"},
            "start": {"type": "integer"},
            "end": {"type": "integer"},
            "text": {"type": "string"},
            "confidence": {"type": "number"},
        }
        schema = {
            "type": "object",
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": fields,
                        "required": list(fields),
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["findings"],
            "additionalProperties": False,
        }
        policy = {
            "mode": "redact_for_search_and_ai",
            "fields": ["body"],
            "categories": [{"name": "email"}],
            "replacement": "category",
            "producer": {
                "type": "generator",
                "revision": "controlled-detector-v1",
                "config": {
                    "generator": {
                        "provider": "openai",
                        "model": "gpt-4.1",
                        "url": "http://provider-a:8090/v1",
                    },
                    "output": "json_schema",
                    "batch": {"max_items_per_call": 1},
                    "output_schema": schema,
                },
            },
        }
        engine.api("/tables/privacy/privacy", "PUT", policy)
        start = len(audit()["calls"])
        engine.api(
            "/tables/privacy/batch",
            "POST",
            {"inserts": {"doc": {"body": CANARY}}, "sync_level": "write"},
        )

        def sanitized():
            cell = engine.api(
                "/query",
                "POST",
                {
                    "table": "privacy",
                    "embeddings": {"vec": [1, 0]},
                    "fields": ["body"],
                    "limit": 10,
                },
            )["responses"][0]
            hits = cell.get("hits", {}).get("hits", [])
            return hits if cell.get("status") == 200 and len(hits) == 1 else None

        hits = wait(sanitized, 40)
        traffic = audit()["calls"][start:]
        embedded = [x for x in traffic if x["task"] == "embedding"]
        detectors = [x for x in traffic if x["task"] == "detector"]
        check(
            phase,
            CANARY not in json.dumps(hits)
            and bool(embedded)
            and bool(detectors)
            and all(x["canary_matches"] == 0 for x in embedded),
            embedder_calls=len(embedded),
            detector_calls=len(detectors),
            detector_originals_expected=True,
        )
        phase = "privacy-fulltext-original-absent"
        result = engine.api(
            "/query",
            "POST",
            {
                "table": "privacy",
                "full_text_search": {"query": 'body:"' + CANARY + '"'},
                "fields": ["body"],
                "limit": 10,
            },
        )["responses"][0]
        check(
            phase,
            result.get("status") == 200 and not result.get("hits", {}).get("hits", []),
        )
        phase = "privacy-pending-backfill"
        configure(detector_hold=True)
        policy["replacement"] = "mask"
        policy["producer"]["revision"] = "controlled-detector-v2"
        status = engine.api("/tables/privacy/privacy")
        if not status.get("etag"):
            raise RuntimeError("privacy policy did not provide its replacement ETag")
        request = urllib.request.Request(
            engine.url + "/tables/privacy/privacy",
            method="PUT",
            data=json.dumps(policy).encode(),
            headers={"Content-Type": "application/json", "If-Match": status["etag"]},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            json.load(response)
        wait(lambda: audit()["pending"])

        def privacy_cell():
            try:
                return engine.api(
                    "/query",
                    "POST",
                    {
                        "table": "privacy",
                        "embeddings": {"vec": [1, 0]},
                        "fields": ["body"],
                        "limit": 10,
                    },
                )["responses"][0]
            except RuntimeError as exc:
                for code in (409, 503):
                    if str(exc).startswith(f"HTTP {code}:"):
                        return {"status": code, "hits": {"hits": []}}
                raise

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            reading = pool.submit(privacy_cell)
            time.sleep(0.25)
            before = reading.result() if reading.done() else None
            unsafe = (
                before is not None
                and before.get("status") == 200
                and bool(before.get("hits", {}).get("hits", []))
            )
            configure(detector_hold=False, release_all=True)
            after = reading.result(timeout=15)
        check(
            phase,
            not unsafe,
            query_blocked_until_release=before is None,
            completed_status=after.get("status"),
        )
        wait(sanitized, 40)
        phase = "privacy-delete"
        engine.api(
            "/tables/privacy/batch",
            "POST",
            {"deletes": ["doc"], "sync_level": "full_index"},
            40,
        )
        check(phase, not privacy_cell().get("hits", {}).get("hits", []))
        success = True
    except Exception as exc:
        checks.append(
            {
                "name": phase,
                "passed": False,
                "error_type": type(exc).__name__,
                "reason": str(exc).replace(owned.token, "[redacted]")[:900],
            }
        )
        print(json.dumps(checks[-1]), flush=True)
    finally:
        unchanged = hashes == {str(p.relative_to(ROOT)): runtime.sha(p) for p in paths}
        success = success and unchanged
        result = {
            "protocol": "extended-v2",
            "engine": "antfly",
            "status": "completed" if success else "execution-incomplete",
            "checks": checks,
            "not_run": [
                "heterogeneous dependency DAG",
                "on-disk decoded audit",
                "physical original erasure",
                "real detector accuracy",
                "exact response-before-commit failpoint",
                "peer extended controls",
            ],
        }
        provenance = {
            "antfly_subject": runtime.subject_provenance(),
            "subject_image": runtime.ANTFLY_IMAGE,
            "client_image": runtime.get_client_image(),
            "scripts_sha256": hashes,
            "source_unchanged_during_run": unchanged,
            "canary_sha256": hashlib.sha256(CANARY.encode()).hexdigest(),
            "scope": "synthetic provider, not real inference performance; original source and detector are permitted privacy surfaces",
        }
        runtime.write_records(out, result, provenance)
        owned.close(success)
    return success


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if run(args.out.resolve()) else 1)
