"""Run the managed PDF page-image to passage alias migration on a dedicated Antfly subject."""

import argparse
import hashlib
import json
import threading
import time
import uuid
from collections import Counter
from pathlib import Path

from compare import observations, replay
from engines import Antfly, call, filters
from evidence import page_id
from native_documents import CONFIG, publish
from pdf_vector_migration import index_config


class PageSearch:
    def __init__(self, subject, parents, index):
        self.subject, self.parents, self.index = subject, parents, index

    def search(self, query, vector, mode, depth):
        terms = []
        for key, value in filters(query).items():
            if key == "tenant":
                continue
            if isinstance(value, int):
                terms.append(
                    {
                        "field": key,
                        "min": value,
                        "max": value,
                        "inclusive_min": True,
                        "inclusive_max": True,
                    }
                )
            else:
                terms.append({"term": {key: value}})
        q = {"limit": depth, "hierarchy": {}, "fields": []}
        if terms:
            q["filter_query"] = terms[0] if len(terms) == 1 else {"conjuncts": terms}
        if mode == "lexical":
            q["full_text_search"] = {"match": query["question"], "field": "text"}
        else:
            q["embeddings"] = {self.index: vector}
        s = self.subject
        response = call(
            s.base + s.path + "/query", "POST", q, s.readers[query["tenant"]]
        )["responses"][0]
        if response["status"] != 200:
            raise RuntimeError(str(response.get("error")))
        hits = []
        for hit in response["hits"]["hits"]:
            h = hit["hierarchy"]
            unit = h.get("parent_unit_id") or h.get("artifact", {}).get("unit_id")
            if not unit or not unit.startswith("page:"):
                raise ValueError("missing physical page citation")
            hits.append(
                (hit["_id"], page_id(self.parents[h["parent_doc_key"]], int(unit[5:])))
            )
        return hits


def run(args):
    manifest = json.loads(
        (
            args.corpus / ("native-plan.json" if args.native_plan else "manifest.json")
        ).read_text()
    )
    docs = json.loads((args.workload / "documents.json").read_text())
    refs = json.loads(args.refs.read_text())
    if not docs.keys() <= refs.keys():
        raise ValueError("source object and tenant inventories differ")
    queries = json.loads((args.corpus / "queries.json").read_text())
    gold = json.loads((args.workload / "judgments.json").read_text())
    subject = Antfly(
        args.endpoint, "pdfnative" + uuid.uuid4().hex[:10], "write", access="api_key"
    )

    def api(path, method="GET", body=None):
        return call(subject.base + subject.path + path, method, body, subject.admin)

    args.out.mkdir(parents=True, exist_ok=False)
    api("", "POST", {"num_shards": 1, "schema": Antfly.schema(True)})
    subject.create_readers()
    subject.readers = {
        tenant: subject.api_key(login.split(":")[0])
        for tenant, login in subject.readers.items()
    }
    subject.admin = subject.api_key(subject.admin.split(":")[0])
    parents = {}

    def coverage(index, expected=None):
        deadline = time.monotonic() + args.timeout
        while True:
            s = api("/indexes/" + index)["status"]
            c = s.get("source_coverage", {})
            n = s.get("searchable_vectors", 0)
            summary = {
                "source_coverage": c,
                "vectors": n,
                "publication": s.get("publication", {}),
            }
            publish(args.out / (index + "-progress.json"), summary)
            if c.get("failed", 0):
                raise RuntimeError(f"{index} reports failed sources: {c}")
            if (
                c.get("healthy")
                and c.get("complete")
                and c["total"] == len(parents)
                and summary["publication"].get("complete")
                and n > 0
                and (expected is None or n == expected)
            ):
                return summary
            if time.monotonic() > deadline:
                raise TimeoutError(f"{index} coverage: {summary}")
            time.sleep(5)

    started = time.monotonic()
    api("/indexes/a", "POST", index_config("native", manifest["model_identity"]))
    batch = {}

    def write():
        ack = api("/batch", "POST", {"inserts": batch, "sync_level": "write"})
        if ack["status"] != "committed":
            raise RuntimeError("source write failed")
        batch.clear()

    for name, tenants in sorted(docs.items()):
        ref = refs[name]
        for tenant, fields in (tenants | {"public": {}}).items():
            key = hashlib.sha256((tenant + "\0" + name).encode()).hexdigest()
            parents[key] = name
            batch[key] = {
                "url": ref["url"],
                "version": ref["etag"],
                "filename": name,
                "mime_type": "application/pdf",
                "tenant": tenant,
                **fields,
            }
            if len(batch) == 32:
                write()
    if batch:
        write()
    ac = coverage("a", manifest["counts"]["a_rows"])
    a_ready = time.monotonic() - started
    alias = "/index-aliases/live"
    api(alias, "PUT", {"index": "a", "require": "complete", "idempotency_key": "start"})
    live = PageSearch(subject, parents, "live")
    direct = PageSearch(subject, parents, "b")
    results = {}
    for phase in ("after-schema-change", "original"):
        results.update(replay(live, "a", queries, args, phase, gold, modes=("dense",)))
    probes = [q for q in queries if q["phase"] == "after-schema-change"]
    probe = probes[args.probe - 1]
    allowed = {q["id"]: set(gold[q["id"]]["eligible"]) for q in probes}

    def read(engine, q, gen, mode):
        return observations(
            engine,
            q,
            gen,
            mode,
            args.depth,
            args.k,
            allowed[q["id"]],
            require_hits=False,
        )

    if not read(live, probe, "a", "dense"):
        raise ValueError("empty initial scoped probe")
    stop = threading.Event()
    traffic = Counter()
    latencies = []

    def serve():
        i = 0
        with (args.out / "live.jsonl").open("w") as stream:
            while not stop.is_set():
                q = probes[i % len(probes)]
                i += 1
                t = time.monotonic()
                pages = []
                try:
                    pages = read(live, q, "a", "dense")
                    state = "served" if pages else "empty"
                except (ValueError, RuntimeError, OSError, KeyError) as exc:
                    state = type(exc).__name__
                latency = time.monotonic() - t
                latencies.append(latency)
                traffic[state] += 1
                stream.write(
                    json.dumps(
                        {
                            "id": q["id"],
                            "seconds": latency,
                            "state": state,
                            "cited_page_returned": bool(
                                set(pages).intersection(gold[q["id"]]["pages"])
                            ),
                        }
                    )
                    + "\n"
                )
                if i % 128 == 0:
                    stream.flush()
                stop.wait(max(0, 0.5 - latency))

    worker = threading.Thread(target=serve)
    worker.start()
    try:
        started = time.monotonic()
        for name, cfg in json.loads(CONFIG.read_text()).items():
            if args.replay_url and name == "document_units_v1":
                producer = json.loads(cfg["producer_json"])
                producer["config"]["ocr"]["reader"] = {
                    "provider": "openai",
                    "base_url": args.replay_url,
                    "model": "officeqa-publisher-replay-v1",
                    "api_key": "loopback-replay",
                }
                cfg["producer_json"] = json.dumps(producer)
            if (
                "sample" in manifest
                and not args.replay_url
                and name == "document_units_v1"
            ):
                producer = json.loads(cfg["producer_json"])
                producer["config"]["ocr"]["reader"]["max_tokens"] = 8192
                cfg["producer_json"] = json.dumps(producer)
            api("/artifacts/" + name + "/enrichment", "PUT", cfg)
        api("/indexes/b", "POST", index_config("passage", manifest["model_identity"]))
        bc = coverage("b")
        if not read(direct, probe, "b", "dense"):
            raise ValueError("empty candidate scoped probe")
        b_ready = time.monotonic() - started
        started = time.monotonic()
        api(
            alias,
            "PUT",
            {
                "index": "b",
                "expected_index": "a",
                "require": "complete",
                "idempotency_key": "promote",
            },
        )
        if api(alias)["index"] != "b" or not read(live, probe, "b", "dense"):
            raise ValueError("alias cutover failed")
        switch = time.monotonic() - started
    finally:
        stop.set()
        worker.join()
    for phase in ("after-schema-change", "original"):
        results.update(replay(live, "b", queries, args, phase, gold))
    extraction = Counter()
    for key in parents:
        m = api("/documents/" + key + "/artifacts/document_units_v1?detail=raw")
        for k in (
            "unit_count",
            "chunk_count",
            "ocr_attempted_count",
            "ocr_selected_count",
            "ocr_failed_count",
        ):
            extraction[k] += m[k]
    replay_counts = (
        call(args.replay_url + "/status", "GET") if args.replay_url else None
    )
    publish(
        args.out / "result.json",
        {
            "engine": "antfly-native",
            "source": manifest,
            "k": args.k,
            "results": results,
            "a_ready_seconds": a_ready,
            "b_ready_seconds": b_ready,
            "cutover_seconds": switch,
            "a_status": ac,
            "b_status": bc,
            "tenant_source_rows": len(parents),
            "extraction": dict(extraction),
            "replay_requests": replay_counts,
            "live_closed_loop": dict(traffic),
            "live_p95_seconds": sorted(latencies)[(95 * len(latencies) - 1) // 100]
            if latencies
            else None,
        },
    )
    return int(
        any(state != "served" for state in traffic)
        or bool(
            replay_counts is not None
            and (not replay_counts.get("mapped") or replay_counts.get("rejected"))
        )
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("workload", "refs", "corpus", "out"):
        p.add_argument("--" + key, required=True, type=Path)
    p.add_argument("--native-plan", action="store_true")
    p.add_argument("--replay-url")
    p.add_argument("--endpoint", required=True)
    p.add_argument("--probe", type=int, default=284)
    p.add_argument("--k", type=int, default=20)
    p.add_argument("--depth", type=int, default=100)
    p.add_argument("--timeout", type=float, default=30 * 86400)
    raise SystemExit(run(p.parse_args()))
