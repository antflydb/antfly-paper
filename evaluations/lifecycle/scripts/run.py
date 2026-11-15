"""Bounded released-image lifecycle controls; no external model calls."""

import argparse
import concurrent.futures
import hashlib
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "evaluations/retrieval/scripts"))
import runtime  # noqa: E402
from fixture import assess, documents  # noqa: E402

runtime.LABEL = "antfly-paper.lifecycle=lifecycle-v1"


def call(url, method="GET", body=None, timeout=15, token=None):
    import urllib.error
    import urllib.request

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(
        url,
        method=method,
        headers=headers,
        data=None if body is None else json.dumps(body).encode(),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"HTTP {exc.code}: {exc.read(600).decode(errors='replace')}"
        ) from exc


def wait(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    raise TimeoutError("bounded lifecycle observation deadline")


class Antfly:
    supports_async = True

    def __init__(self, owned):
        self.owned = owned
        self.revisions = {}
        config = owned.output / "config.json"
        config.write_text(json.dumps({"inference": {"api_url": "http://127.0.0.1:1"}}))
        volume = owned.volume("antfly-data")
        self.name = owned.container(
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
            ulimits=["nofile=65536:524288"],
        )
        self.url = f"http://127.0.0.1:{owned.port(self.name, 8080)}/db/v1"
        self.ready()
        self.api("/tables/lifecycle", "POST", {"num_shards": 1})

    def ready(self):
        def probe():
            try:
                return isinstance(self.api("/tables"), list)
            except (OSError, ValueError):
                return False

        wait(probe)

    def api(self, path, method="GET", body=None, timeout=20):
        return call(self.url + path, method, body, timeout)

    def generation(self, name, revision):
        self.revisions[name] = revision
        return self.api(
            "/tables/lifecycle/indexes/" + name,
            "POST",
            {
                "type": "embeddings",
                "dimension": 2,
                "field": "body",
                "publication_policy": "atomic",
                "embedder": {
                    "provider": "antfly",
                    "model": "lifecycle",
                    "api_url": f"http://provider-{revision.lower()}:8090",
                    "model_identity": ("a" if revision == "A" else "b") * 64,
                },
            },
        )

    def write(self, identity, row, sync="full_index"):
        result = self.api(
            "/tables/lifecycle/batch",
            "POST",
            {"inserts": {identity: row}, "sync_level": sync},
            40,
        )
        if result.get("status") not in {"committed", "committed_pending"}:
            raise ValueError("unacknowledged source write")

    def delete(self, identity):
        return self.api(
            "/tables/lifecycle/batch",
            "POST",
            {"deletes": [identity], "sync_level": "write"},
        )

    def query(self, name):
        response = self.api(
            "/query",
            "POST",
            {
                "table": "lifecycle",
                "indexes": [name],
                "embeddings": {name: [1, 0] if self.revisions[name] == "A" else [0, 1]},
                "fields": ["*", "_embeddings"],
                "limit": 100,
            },
        )["responses"][0]
        if response.get("status") != 200:
            raise RuntimeError("query unavailable")
        return {
            h["_id"]: h["_source"]["_embeddings"][name]
            for h in response.get("hits", {}).get("hits", [])
        }

    def complete(self, name):
        return (
            self.api("/tables/lifecycle/indexes/" + name)
            .get("status", {})
            .get("coverage", {})
            .get("complete", False)
        )

    def restart(self):
        runtime.docker(["kill", self.name])
        runtime.docker(["start", self.name])
        self.url = f"http://127.0.0.1:{self.owned.port(self.name, 8080)}/db/v1"
        self.ready()


def run(out, engine_name="antfly"):
    instrument_paths = [
        *HERE.glob("*.py"),
        Path(runtime.__file__),
        runtime.SUBJECT_IMPLEMENTATION,
        HERE.parent / "subjects.json",
    ]

    def instrument_hashes():
        return {str(p.relative_to(ROOT)): runtime.sha(p) for p in instrument_paths}

    source_hashes = instrument_hashes()
    owned = runtime.Owned(out)

    # This controller observes published loopback ports; Docker internal-only
    # networks do not publish them on current Desktop releases.
    def start():
        owned.output.mkdir(parents=True, exist_ok=False)
        runtime.create_network(owned.network, internal=False)

    owned.start = start
    owned.start()
    checks = []
    phase = "start"
    counts = {
        "scored_queries": 0,
        "post_cutover_vector_mismatches": 0,
        "post_barrier_stale_hits": 0,
    }
    terminal = "execution-incomplete"
    providers = {}

    def record(name, passed, **details):
        checks.append({"name": name, "passed": bool(passed), **details})
        print(json.dumps(checks[-1]), flush=True)
        if not passed:
            raise AssertionError(name)

    try:
        for revision in ("A", "B"):
            name = owned.client(
                "provider-" + revision.lower(),
                ["/lifecycle/provider.py"],
                env=["PAPER_TOKEN"],
                mounts=[f"type=bind,src={HERE},dst=/lifecycle,readonly"],
                ports=[8090],
                cpus=0.5,
                memory="256m",
            )
            url = f"http://127.0.0.1:{owned.port(name, 8090)}"
            providers[revision] = url

            def ready(url=url):
                try:
                    return call(url + "/control", token=owned.token)
                except OSError:
                    return False

            wait(ready)
            call(
                url + "/control",
                "POST",
                {"revision": revision, "mode": "ok"},
                token=owned.token,
            )

        def audit(revision="A"):
            return call(providers[revision] + "/control", token=owned.token)

        def control(mode, revision="A", attest=None):
            call(
                providers[revision] + "/control",
                "POST",
                {"revision": attest or revision, "mode": mode},
                token=owned.token,
            )

        def release(ids, revision="A"):
            call(
                providers[revision] + "/release",
                "POST",
                {"ids": ids},
                token=owned.token,
            )

        if engine_name == "vespa":
            from vespa import Vespa

            engine = Vespa(owned, call, wait, runtime.docker)
        elif engine_name == "opensearch":
            from opensearch import OpenSearch

            engine = OpenSearch(owned, call, wait, runtime.docker)
        elif engine_name == "postgres-outbox":
            from postgres import Postgres

            engine = Postgres(
                owned, call, wait, runtime.docker, HERE, runtime.PGVECTOR_IMAGE
            )
        elif engine_name == "lancedb":
            from lance import Lance

            engine = Lance(owned, call, wait, runtime.docker, HERE)
        else:
            engine = Antfly(owned)
        ledger = documents()

        def current(index, revision):
            hits = engine.query(index)
            counts["scored_queries"] += 1
            return assess(hits, ledger, revision)["passed"]

        phase = "initial-derived-ready"
        engine.generation("a0", "A")
        for identity, row in ledger.items():
            engine.write(identity, row)
        wait(lambda: current("a0", "A"))
        record(phase, True, documents=len(ledger))
        phase = "metadata-only-reuse"
        before = len(audit()["calls"])
        ledger["doc0"]["tag"] = "metadata"
        if hasattr(engine, "metadata"):
            engine.metadata("doc0", ledger["doc0"])
        else:
            engine.write("doc0", ledger["doc0"])
        wait(lambda: current("a0", "A"))
        time.sleep(0.5)
        record(phase, len(audit()["calls"]) == before)
        if engine.supports_async:
            phase = "held-source-update"
            control("hold")
            engine.write(
                "doc0", {"body": "superseded held source", "version": 2}, "write"
            )
            held = wait(lambda: audit()["pending"])
            control("ok")
            ledger["doc0"] = {"body": "latest source version three", "version": 3}
            engine.write("doc0", ledger["doc0"], "write")
            release(held)
            wait(lambda: current("a0", "A"))
            for _ in range(10):
                record_value = current("a0", "A")
                counts["post_barrier_stale_hits"] += int(not record_value)
                time.sleep(0.02)
            record(phase, counts["post_barrier_stale_hits"] == 0)
            phase = "held-source-delete"
            control("hold")
            engine.write("doc1", {"body": "deleted held source", "version": 2}, "write")
            held = wait(lambda: audit()["pending"])
            engine.delete("doc1")
            del ledger["doc1"]
            control("ok")
            release(held)
            wait(lambda: current("a0", "A"))
            time.sleep(0.3)
            record(phase, current("a0", "A"))
            phase = "after-publication-crash"
            engine.restart()
            record(phase, wait(lambda: current("a0", "A")))
            phase = "crash-with-request-held"
            control("hold")
            ledger["doc2"] = {"body": "crash held latest source", "version": 2}
            engine.write("doc2", ledger["doc2"], "write")
            held = wait(lambda: audit()["pending"])
            engine.restart()
            control("ok")
            still = audit()["pending"]
            if still:
                release(still)
            record(phase, wait(lambda: current("a0", "A")))
        else:
            phase = "synchronous-update-delete-restart"
            ledger["doc0"] = {"body": "latest synchronous source", "version": 2}
            engine.write("doc0", ledger["doc0"])
            engine.delete("doc1")
            del ledger["doc1"]
            wait(lambda: current("a0", "A"))
            engine.restart()
            record(phase, wait(lambda: current("a0", "A")))
        active, active_revision = "a0", "A"
        for name, revision in [("b1", "B"), ("a2", "A")]:
            phase = "migration-" + name
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(engine.generation, name, revision)
                during = 0
                old_mismatches = 0
                while not future.done():
                    observed = assess(engine.query(active), ledger, active_revision)
                    counts["scored_queries"] += 1
                    old_mismatches += (
                        observed["missing"]
                        + observed["unexpected"]
                        + observed["vector_mismatches"]
                    )
                    during += 1
                    time.sleep(0.02)
                future.result()
            wait(lambda name=name: engine.complete(name), 180)
            wait(lambda name=name, revision=revision: current(name, revision))
            for _ in range(10):
                hits = engine.query(name)
                counts["scored_queries"] += 1
                observed = assess(hits, ledger, revision)
                counts["post_cutover_vector_mismatches"] += (
                    observed["vector_mismatches"]
                    + observed["missing"]
                    + observed["unexpected"]
                )
            record(
                phase,
                counts["post_cutover_vector_mismatches"] == 0 and old_mismatches == 0,
                old_generation_vector_mismatches=old_mismatches,
                old_generation_queries=during,
                documents=len(ledger),
                cutover="client selects index and query encoder",
            )
            active, active_revision = name, revision
        terminal = "completed"
    except Exception as exc:
        checks.append(
            {
                "name": phase,
                "passed": False,
                "error_type": type(exc).__name__,
                "reason": str(getattr(exc, "output", None) or exc)
                .replace(owned.password, "[redacted]")
                .replace(owned.token, "[redacted]")[:1600],
            }
        )
        print(json.dumps(checks[-1]), flush=True)
    finally:
        success = terminal == "completed" and all(c["passed"] for c in checks)
        unchanged = source_hashes == instrument_hashes()
        if not unchanged:
            terminal = "instrument-changed-during-run"
            success = False
        provenance = {
            "antfly_subject": runtime.subject_provenance(),
            "antfly_subject_tag": runtime.SUBJECT_TAG
            if engine_name == "antfly"
            else None,
            "server_image": runtime.ANTFLY_IMAGE
            if engine_name == "antfly"
            else __import__("vespa").IMAGE
            if engine_name == "vespa"
            else __import__("opensearch").IMAGE
            if engine_name == "opensearch"
            else runtime.PGVECTOR_IMAGE
            if engine_name == "postgres-outbox"
            else runtime.get_client_image(),
            "engine_subject": runtime.subject_provenance()
            if engine_name == "antfly" and runtime.CANDIDATE
            else json.loads((HERE.parent / "subjects.json").read_text())[engine_name],
            "client_image": runtime.get_client_image(),
            "scripts_sha256": source_hashes,
            "source_unchanged_during_run": unchanged,
            "subjects_manifest_sha256": runtime.sha(HERE.parent / "subjects.json"),
            "requirements_sha256": runtime.sha(runtime.CAMPAIGN / "requirements.lock"),
            "nofile": "65536:524288",
            "provider": "deterministic synthetic fixture; not inference performance",
        }
        result = {
            "protocol": "lifecycle-v1",
            "engine": engine_name,
            "status": terminal,
            "checks": checks,
            "observations": counts,
            "not_run": [
                *(
                    ["async-source-acknowledgement races"]
                    if engine_name not in {"antfly", "postgres-outbox"}
                    else []
                ),
                "concurrent source writes during generation builds",
                "held old replies across generation cutovers",
                "crash during generation migration",
                "wrong or missing provider revision acknowledgement",
                "response-before-commit failpoint",
                "isolated inference-worker crash",
                "committed nondeterministic decision replay",
                "heterogeneous producer chain",
                "distributed cutover",
                "real-model performance",
                "privacy inventory",
                "untrusted policy attacks",
            ],
            "fixture_sha256": hashlib.sha256(
                json.dumps(documents(), sort_keys=True).encode()
            ).hexdigest(),
        }
        runtime.write_records(out, result, provenance)
        owned.close(success)
    return success


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--engine",
        choices=["antfly", "vespa", "lancedb", "postgres-outbox", "opensearch"],
        default="antfly",
    )
    args = parser.parse_args()
    raise SystemExit(0 if run(args.out.resolve(), args.engine) else 1)
