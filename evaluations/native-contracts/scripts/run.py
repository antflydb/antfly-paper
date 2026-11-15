"""Executed standalone contracts for main: aliases, direct gates and scope AND."""

import argparse
import base64
import concurrent.futures
import json
import sys
from pathlib import Path

from http_client import http, wait

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.append(str(ROOT / "evaluations/retrieval/scripts"))
import runtime  # noqa: E402

runtime.LABEL = "antfly-paper.contracts=main-v1"


def run(out):
    if not runtime.CANDIDATE:
        raise ValueError("new contracts require an explicit candidate subject")
    owned = runtime.Owned(out)
    out.mkdir(parents=True, exist_ok=False)
    runtime.create_network(owned.network, internal=False)
    paths = [
        *HERE.glob("*.py"),
        *(ROOT / "evaluations/lifecycle-extended/scripts").glob("*.py"),
        *(ROOT / "evaluations/lifecycle/scripts").glob("*.py"),
        Path(runtime.__file__),
        runtime.SUBJECT_IMPLEMENTATION,
    ]
    hashes = {str(p.relative_to(ROOT)): runtime.sha(p) for p in paths}
    checks = []
    success = False
    phase = "setup"

    def check(name, passed, **details):
        record = {"name": name, "passed": bool(passed), **details}
        checks.append(record)
        print(json.dumps(record), flush=True)
        if not passed:
            raise AssertionError(name)

    try:
        endpoints = {}
        for revision in ("a", "b"):
            name = owned.client(
                "provider-" + revision,
                ["/native/provider.py"],
                env=["PAPER_TOKEN"],
                mounts=[
                    f"type=bind,src={HERE},dst=/native,readonly",
                    f"type=bind,src={ROOT / 'evaluations/lifecycle-extended/scripts'},dst=/extended,readonly",
                    f"type=bind,src={ROOT / 'evaluations/lifecycle/scripts'},dst=/shared,readonly",
                ],
                ports=[8090],
                cpus=0.5,
                memory="256m",
            )
            endpoints[revision] = f"http://127.0.0.1:{owned.port(name, 8090)}"

        def control(revision, body=None):
            status, value = http(
                endpoints[revision] + "/control",
                body,
                headers={"Authorization": "Bearer " + owned.token},
            )
            if status != 200:
                raise ValueError("provider control rejected")
            return value

        def providers_ready():
            try:
                return all(control(r) for r in endpoints)
            except OSError:
                return False

        wait(providers_ready)
        control("b", {"revision": "B"})
        config = out / "config.json"
        config.write_text(
            json.dumps({"inference": {"api_url": "http://provider-a:8090"}})
        )
        owned.environment["ANTFLY_BOOTSTRAP_ADMIN_PASSWORD"] = owned.password
        owned.environment["OPENAI_API_KEY"] = "synthetic-fixture"
        volume = owned.volume("database")
        server = owned.container(
            "antfly",
            runtime.ANTFLY_IMAGE,
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
                "/antflydb",
                "--config",
                "/config.json",
            ],
            env=["ANTFLY_BOOTSTRAP_ADMIN_PASSWORD", "OPENAI_API_KEY"],
            mounts=[
                f"type=volume,src={volume},dst=/antflydb",
                f"type=bind,src={config},dst=/config.json,readonly",
            ],
            ports=[8080],
            cpus=3,
            memory="6g",
            ulimits=["nofile=65536:524288"],
        )
        url = f"http://127.0.0.1:{owned.port(server, 8080)}"

        def api(path, body=None, method=None, user="admin", password=None):
            return http(
                url + path,
                body,
                method,
                {
                    "Authorization": "Basic "
                    + base64.b64encode(
                        (user + ":" + (password or owned.password)).encode()
                    ).decode()
                },
            )

        def ok(path, body=None, method=None, **kwargs):
            status, value = api(path, body, method, **kwargs)
            if status not in (200, 201, 202):
                raise ValueError(f"HTTP {status} {path}: {str(value)[:500]}")
            return value

        def ready():
            try:
                return api("/db/v1/tables")[0] == 200
            except OSError:
                return False

        wait(ready)

        def query(table, **fields):
            return ok("/db/v1/query", {"table": table, "limit": 10, **fields})[
                "responses"
            ][0]

        def top(result):
            return (
                result["hits"]["hits"][0]["_id"]
                if result.get("status") == 200 and result.get("hits", {}).get("hits")
                else None
            )

        phase = "alias-routing"
        ok("/db/v1/tables/spaces", {"num_shards": 1})
        for generation, revision in (("a0", "a"), ("b1", "b")):
            ok(
                "/db/v1/tables/spaces/indexes/" + generation,
                {
                    "type": "embeddings",
                    "dimension": 2,
                    "distance_metric": "cosine",
                    "field": "body",
                    "publication_policy": "atomic",
                    "embedder": {
                        "provider": "antfly",
                        "model": revision.upper(),
                        "api_url": "http://provider-" + revision + ":8090",
                        "model_identity": revision * 64,
                    },
                },
            )
        setup_ack = ok(
            "/db/v1/tables/spaces/batch",
            {
                "inserts": {
                    "red": {"body": "red-source"},
                    "blue": {"body": "blue-source"},
                },
                "sync_level": "full_index",
            },
        )

        # Alias admission consumes the asynchronously reported durable coverage
        # proof, rather than the source-write response alone.
        def targets_ready():
            return all(
                ok("/db/v1/tables/spaces/indexes/" + name)
                .get("status", {})
                .get("coverage", {})
                .get("complete")
                for name in ("a0", "b1")
            )

        wait(targets_ready)
        check("alias-targets-report-complete", targets_ready(), batch_ack=setup_ack)
        alias = "/db/v1/tables/spaces/index-aliases/live"
        first = {"index": "a0", "require": "complete", "idempotency_key": "create-a"}
        ok(alias, first, "PUT")
        initial = query(
            "spaces", indexes=["live"], semantic_search="red-query", fields=["body"]
        )
        check(
            "alias-initial-model",
            top(initial) == "red" and initial.get("resolved_indexes") == ["a0"],
        )
        control("a", {"embedding_hold": True})
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(
                query,
                "spaces",
                indexes=["live"],
                semantic_search="red-held-query",
                fields=["body"],
            )
            wait(lambda: control("a")["pending"])
            swap = {
                "index": "b1",
                "expected_index": "a0",
                "require": "complete",
                "idempotency_key": "swap-b",
            }
            ok(alias, swap, "PUT")
            control("a", {"embedding_hold": False, "release_all": True})
            old = pending.result(timeout=60)
        new = query(
            "spaces", indexes=["live"], semantic_search="red-new-query", fields=["body"]
        )
        check(
            "alias-held-old-query-pins-index-and-encoder",
            top(old) == top(new) == "red"
            and old.get("resolved_indexes") == ["a0"]
            and new.get("resolved_indexes") == ["b1"],
        )
        wrong = query(
            "spaces", indexes=["b1"], embeddings={"b1": [1, 0]}, fields=["body"]
        )
        check("alias-wrong-encoder-negative-control", top(wrong) == "blue")
        status, _ = api(
            alias,
            {"index": "a0", "expected_index": "a0", "idempotency_key": "stale"},
            "PUT",
        )
        check("alias-stale-cas-rejected", status == 409)
        ok(
            alias,
            {"index": "a0", "expected_index": "b1", "idempotency_key": "back-a"},
            "PUT",
        )
        receipt = ok(alias, swap, "PUT")
        check(
            "alias-old-receipt-does-not-reapply-swap",
            receipt["index"] == "b1" and ok(alias)["index"] == "a0",
        )
        runtime.docker(["kill", server])
        runtime.docker(["start", server])
        url = f"http://127.0.0.1:{owned.port(server, 8080)}"
        wait(ready)
        check(
            "alias-survives-restart",
            ok(alias)["index"] == "a0"
            and top(
                query("spaces", indexes=["live"], semantic_search="red-restarted-query")
            )
            == "red",
        )
        phase = "direct-enrichment-gates"
        schema = {
            "type": "object",
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "category": {"type": "string"},
                            "start": {"type": "integer"},
                            "end": {"type": "integer"},
                            "text": {"type": "string"},
                            "confidence": {"type": "number"},
                        },
                        "required": ["category", "start", "end", "text", "confidence"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["findings"],
            "additionalProperties": False,
        }
        for table in ("held", "gates", "audit"):
            ok("/db/v1/tables/" + table, {"num_shards": 1})

        def producer(table, output_schema):
            ok(
                f"/db/v1/tables/{table}/artifacts/decision/enrichment",
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
                            "output_schema": output_schema,
                        },
                    },
                },
                "PUT",
            )

        producer("held", schema)
        control("a", {"detector_hold": True})
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            commit = pool.submit(
                ok,
                "/db/v1/tables/held/batch",
                {
                    "commit_requires": "direct_enrichments",
                    "sync_level": "write",
                    "inserts": {"doc": {"body": "lifecycle-canary@example.invalid"}},
                },
            )
            wait(lambda: control("a")["pending"])
            hidden = api("/db/v1/tables/held/documents/doc")[0]
            control("a", {"detector_hold": False, "release_all": True})
            ack = commit.result(timeout=60)
        artifact = ok("/db/v1/tables/held/documents/doc?fields=_artifacts.decision")
        check(
            "direct-gate-hides-source-until-producer-completes",
            hidden == 404
            and ack["status"].startswith("committed")
            and bool(artifact.get("_artifacts", {}).get("decision")),
        )
        producer(
            "gates",
            {
                "type": "object",
                "properties": {"verdict": {"type": "string"}},
                "required": ["verdict"],
                "additionalProperties": False,
            },
        )
        ok(
            "/db/v1/tables/gates/batch",
            {
                "commit_requires": "direct_enrichments",
                "sync_level": "write",
                "inserts": {"old": {"body": "old"}, "victim": {"body": "retain"}},
            },
        )
        control("a", {"decision_fail": True})
        status, _ = api(
            "/db/v1/batch",
            {
                "commit_requires": "direct_enrichments",
                "sync_level": "write",
                "tables": {
                    "gates": {
                        "inserts": {"old": {"body": "rejected"}},
                        "deletes": ["victim"],
                    },
                    "audit": {"inserts": {"accepted": {"body": "must not commit"}}},
                },
            },
        )
        check(
            "direct-gate-failure-aborts-all-transaction-participants",
            status >= 400
            and ok("/db/v1/tables/gates/documents/old")["body"] == "old"
            and api("/db/v1/tables/gates/documents/victim")[0] == 200
            and api("/db/v1/tables/audit/documents/accepted")[0] == 404,
        )
        control("a", {"decision_fail": False})
        phase = "scope-conjunction"
        ok("/db/v1/tables/scoped", {"num_shards": 1})
        ok(
            "/db/v1/tables/scoped/batch",
            {
                "inserts": {
                    "both": {"tenant": "alpha", "tier": "gold"},
                    "tenant-only": {"tenant": "alpha", "tier": "silver"},
                    "tier-only": {"tenant": "beta", "tier": "gold"},
                },
                "sync_level": "full_index",
            },
        )
        reader_password = owned.token
        ok(
            "/auth/v1/users/reader",
            {
                "password": reader_password,
                "initial_policies": [
                    {"resource": "scoped", "resource_type": "table", "type": "read"}
                ],
            },
        )
        ok("/auth/v1/users/reader/row-filters/*", {"term": {"tenant": "alpha"}}, "PUT")
        ok(
            "/auth/v1/users/reader/row-filters/scoped",
            {"term": {"tier": "gold"}},
            "PUT",
        )
        result = ok(
            "/db/v1/tables/scoped/query",
            {"full_text_search": {"match_all": {}}, "limit": 10},
            user="reader",
            password=reader_password,
        )["responses"][0]
        check(
            "overlapping-global-and-table-filters-conjoin",
            {h["_id"] for h in result["hits"]["hits"]} == {"both"},
        )
        check(
            "scope-conjunction-applies-to-direct-lookup",
            all(
                api(
                    "/db/v1/tables/scoped/documents/" + key,
                    user="reader",
                    password=reader_password,
                )[0]
                == 404
                for key in ("tenant-only", "tier-only")
            ),
        )
        ok(
            "/db/v1/tables/scoped/batch",
            {
                "inserts": {
                    "namespace-hidden": {
                        "tenant": "alpha",
                        "tier": "gold",
                        "visibility": "private",
                    },
                    "role-hidden": {
                        "tenant": "alpha",
                        "tier": "gold",
                        "visibility": "public",
                        "project": "blocked",
                    },
                    "both": {
                        "tenant": "alpha",
                        "tier": "gold",
                        "visibility": "public",
                        "project": "approved",
                    },
                },
                "sync_level": "full_index",
            },
        )

        def reader_ids():
            value = ok(
                "/db/v1/tables/scoped/query",
                {"full_text_search": {"match_all": {}}, "limit": 10},
                user="reader",
                password=reader_password,
            )["responses"][0]
            return {h["_id"] for h in value["hits"]["hits"]}

        check(
            "additional-scope-positive-control",
            reader_ids() == {"both", "namespace-hidden", "role-hidden"},
        )
        ok(
            "/auth/v1/users/reader/row-filters/*?database=default&namespace=public&all_tables=true",
            {"term": {"visibility": "public"}},
            "PUT",
        )
        check(
            "namespace-filter-conjoins-with-global-and-table",
            reader_ids() == {"both", "role-hidden"},
        )
        ok("/auth/v1/users/reader/roles", {"role": "role:paper_reader"})
        ok(
            "/auth/v1/subjects/role:paper_reader/row-filters/scoped",
            {"term": {"project": "approved"}},
            "PUT",
        )
        check(
            "inherited-role-filter-conjoins-with-user-scopes", reader_ids() == {"both"}
        )
        success = True
    except Exception as exc:
        (out / "failure.json").write_text(
            json.dumps(
                {
                    "phase": phase,
                    "error": type(exc).__name__,
                    "message": str(exc)[:1000],
                },
                indent=2,
            )
            + "\n"
        )
        raise
    finally:
        result = {
            "protocol": "native-contracts-main-v1",
            "status": "completed" if success else "execution-incomplete",
            "checks": checks,
            "not_run": [
                "embedded C ABI",
                "Raft",
                "serverless object store",
            ],
            "source_unchanged": hashes
            == {str(p.relative_to(ROOT)): runtime.sha(p) for p in paths},
        }
        runtime.write_records(
            out,
            result,
            {
                "subject": runtime.subject_provenance(),
                "source_hashes": hashes,
                "scope": "standalone HTTP only; synthetic causal controls",
            },
        )
        owned.close(success)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, required=True)
    run(p.parse_args().out.resolve())
