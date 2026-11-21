"""Qualify the OfficeQA PDF representation migration on public, single-page sources."""

import argparse
import functools
import hashlib
import http.server
import json
import math
import os
import secrets
import threading
import time
from contextlib import closing
from pathlib import Path

from engines import ApiKey, call
from fixture_archive import materialize, native_pdf
from native_documents import CONFIG, publish

MODEL = "google/embeddinggemma-2:bf16-safetensors-bundle-v1"
QUESTION = "Which document describes the keeper inspecting each brass lantern?"
NAMES = ("ledger", "appendix", "long")


def index_config(mode, identity):
    embedder = {"provider": "antfly", "model": MODEL, "model_identity": identity}
    cfg = {
        "type": "embeddings",
        "dimension": 768,
        "distance_metric": "cosine",
        "embedder": embedder,
    }
    if mode == "rendered":
        return cfg | {"template": "{{remoteMedia url=image_url}}"}
    visual = mode == "native"
    name = "page_image" if visual else "text_passage"
    source = (
        {"embedding_input": "pdf_page_images"}
        if visual
        else {"source_artifact_name": "document_chunks_v1"}
    )
    enrichment = {
        "name": name,
        "kind": "embedding",
        "expected_dims": 768,
        "field": "url" if visual else "text",
        **source,
    }
    if not visual:
        cfg.update(source)
    return cfg | {
        "field": "embedding",
        "embedding_name": name,
        "enrichments": [enrichment],
    }


def prepare(directory):
    import pypdfium2 as pdfium

    materialize(directory)
    (directory / "v1/long.pdf").write_bytes(
        native_pdf(
            "The keeper inspected each brass lantern and entered measurements into the ship ledger. "
            * 19
        )
    )
    start = time.monotonic()
    for path in sorted(directory.glob("v*/*.pdf")):
        with closing(pdfium.PdfDocument(path)) as pdf:
            if len(pdf) != 1:
                raise ValueError(
                    "the application rendering control requires one page per source"
                )
            with closing(pdf[0]) as page:
                page.render(scale=2).to_pil().save(path.with_suffix(".png"))
    elapsed = time.monotonic() - start
    # This additional source checks that citations distinguish pages of a single PDF.
    with closing(pdfium.PdfDocument.new()) as combined:
        for text in (
            "The orchard crew inspected the apple trees and pruned damaged branches before spring.",
            "The instrument register contains a xenolith spectrometer calibration and its laboratory serial number.",
        ):
            with closing(pdfium.PdfDocument(native_pdf(text))) as source:
                combined.import_pages(source)
        combined.save(directory / "two-pages.pdf")
    return elapsed


def wait(check, description, timeout=90):
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise TimeoutError(description)
        time.sleep(0.2)


def coverage(api, name):
    s = api("/indexes/" + name)["status"]
    c = s["source_coverage"]
    return {
        "vectors": s["searchable_vectors"],
        "source_total": c["total"],
        "covered": c["covered"],
        "healthy": c["healthy"],
        "complete": c["complete"],
        "publication_complete": s.get("publication", {}).get("complete", False),
    }


def ready(api, name, count):
    def check():
        c = coverage(api, name)
        return (
            c
            if (
                c["source_total"] == c["covered"] == 3
                and c["vectors"] == count
                and c["complete"]
                and c["healthy"]
                and c["publication_complete"]
            )
            else None
        )

    return wait(check, name + " source coverage and vector publication")


def run(args, pdf_url, mode, directory):
    base = args.endpoint.rstrip("/")
    table = "pdfmigration" + secrets.token_hex(5)
    path = base + "/db/v1/tables/" + table

    def api(suffix, method="GET", body=None, *, auth=args.auth):
        return call(path + suffix, method, body, auth)

    def doc(name, revision=1, tenant="public"):
        relative = f"v{revision}/{name}.pdf"
        return {
            "url": pdf_url + relative,
            "filename": name + ".pdf",
            "version": hashlib.sha256((directory / relative).read_bytes()).hexdigest(),
            "mime_type": "application/pdf",
            "tenant": tenant,
            "page_number": 1,
            "image_url": pdf_url + relative.removesuffix(".pdf") + ".png",
        }

    def write(rows):
        r = api("/batch", "POST", {"inserts": rows, "sync_level": "write"})
        if r["status"] != "committed":
            raise RuntimeError("fixture write failed")

    result = {"initial_representation": mode, "model": MODEL, "dimensions": 768}
    response = call(
        base + "/ai/v1/embed",
        "POST",
        {
            "model": MODEL,
            "input": [QUESTION],
            "dimensions": 768,
            "task_type": "RETRIEVAL_QUERY",
        },
        args.auth,
    )
    identity = result["model_identity"] = response["model_identity"]
    result["backend"] = response.get("backend")
    vector = response["data"][0]["embedding"]
    if len(vector) != 768 or not all(math.isfinite(v) for v in vector):
        raise ValueError("invalid query embedding")

    api(
        "",
        "POST",
        {
            "num_shards": 1,
            "schema": {
                "default_type": "default",
                "document_schemas": {
                    "default": {
                        "schema": {
                            "type": "object",
                            "additionalProperties": True,
                            "properties": {
                                "tenant": {
                                    "type": "string",
                                    "x-antfly-field": {"type": "keyword"},
                                }
                            },
                        }
                    }
                },
            },
        },
    )
    owner = table + "reader"
    call(
        base + "/auth/v1/users/" + owner,
        "POST",
        {
            "password": secrets.token_hex(16),
            "initial_policies": [
                {"resource": table, "type": "read", "resource_type": "table"}
            ],
        },
        args.auth,
    )
    call(
        base + "/auth/v1/users/" + owner + "/row-filters/" + table,
        "PUT",
        {"term": {"tenant": "public"}},
        args.auth,
    )
    key = call(
        base + "/auth/v1/users/" + owner + "/api-keys",
        "POST",
        {"name": "fixture"},
        args.auth,
    )
    reader = ApiKey(key["key_id"], key["key_secret"])

    def search(
        index,
        auth,
        *,
        profile=False,
        hierarchy=None,
        lexical=None,
        hybrid=False,
        query_vector=None,
    ):
        q = {
            "fields": ["filename", "page_number", "version"],
            "limit": 10,
            "profile": profile,
        }
        if lexical is None or hybrid:
            q["embeddings"] = {index: vector if query_vector is None else query_vector}
        if lexical is not None:
            q["full_text_search"] = {"query": lexical}
        if hierarchy is not None:
            q["hierarchy"] = hierarchy
        response = api("/query", "POST", q, auth=auth)["responses"][0]
        if response["status"] != 200:
            raise RuntimeError(str(response.get("error")))
        hits = []
        for hit in response["hits"]["hits"]:
            h = hit.get("hierarchy", {})
            key = h.get("parent_doc_key", hit["_id"])
            art = h.get("matched_artifact") or h.get("artifact", {})
            unit = art.get("unit_id") or h.get("parent_unit_id")
            page = (
                int(unit.removeprefix("page:"))
                if unit
                else hit.get("_source", {}).get("page_number")
                if mode == "rendered" and index in ("a", "live")
                else None
            )
            hits.append({"source": key, "page": page, "artifact": art.get("name")})
        raw = response.get("profile", {}).get("dense_search", {}).get("raw_hit_count")
        return {"hits": hits, "raw_hits": raw}

    start = time.monotonic()
    api("/indexes/a", "POST", index_config(mode, identity))
    write({n: doc(n) for n in NAMES})
    result["a_coverage"] = ready(api, "a", 3)
    result["a_build_seconds"] = time.monotonic() - start
    alias = "/index-aliases/live"
    api(alias, "PUT", {"index": "a", "require": "complete", "idempotency_key": "open"})
    result["a_admin"] = search("a", args.auth, profile=True)
    result["a_direct"] = search("a", args.auth, hierarchy={}, profile=True)
    result["a_reader"] = search("live", reader)

    samples = []
    stopped = threading.Event()

    def traffic():
        while not stopped.is_set():
            t = time.monotonic()
            try:
                hit = search("live", reader)["hits"]
                state = "ok" if hit else "empty"
            except (RuntimeError, OSError) as e:
                state = str(e)
            samples.append((time.monotonic() - t, state))
            stopped.wait(0.1)

    worker = threading.Thread(target=traffic)
    worker.start()
    try:
        start = time.monotonic()
        for n, c in json.loads(CONFIG.read_text()).items():
            api("/artifacts/" + n + "/enrichment", "PUT", c)
        api("/indexes/b", "POST", index_config("passage", identity))
        result["b_before_mutation"] = coverage(api, "b")
        write(
            {"ledger": doc("ledger", 2), "appendix": doc("appendix", tenant="private")}
        )
        result["b_coverage"] = ready(api, "b", 4)

        def corrected():
            manifest = api("/documents/ledger/artifacts/document_units_v1?detail=raw")
            return (
                manifest.get("source_url") == doc("ledger", 2)["url"]
                and manifest.get("ocr_selected_count") == 1
                and bool(search("b", reader, lexical="rope")["hits"])
            )

        wait(corrected, "corrected scanned source and searchable rope")
        result["b_build_and_correction_seconds"] = time.monotonic() - start
        result["b_direct"] = search("b", args.auth, hierarchy={}, profile=True)
        result["b_admin"] = search("b", args.auth, profile=True)
        result["b_reader"] = search("b", reader)
        result["b_hybrid_reader"] = search("b", reader, lexical=QUESTION, hybrid=True)
        result["correction_terms"] = {
            word: [h["source"] for h in search("b", reader, lexical=word)["hits"]]
            for word in ("rope", "lamps")
        }
        result["extraction"] = {}
        for name in NAMES:
            m = api("/documents/" + name + "/artifacts/document_units_v1?detail=raw")
            result["extraction"][name] = {
                k: m[k]
                for k in (
                    "unit_count",
                    "chunk_count",
                    "ocr_selected_count",
                    "ocr_failed_count",
                )
            }
        start = time.monotonic()
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
        result["alias_swap_seconds"] = time.monotonic() - start
        result["alias_after"] = api(alias)["index"]
        result["after_reader"] = search("live", reader)
    finally:
        stopped.set()
        worker.join()
    latencies = sorted(t * 1000 for t, _ in samples)
    result["traffic"] = {
        "requests": len(samples),
        "empty": sum(state == "empty" for _, state in samples),
        "errors": [s for _, s in samples if s not in ("ok", "empty")],
        "p95_ms": latencies[math.ceil(len(latencies) * 0.95) - 1]
        if latencies
        else None,
    }

    def pages(which):
        return {(h["source"], h["page"]) for h in result[which]["hits"]}

    original = {(n, 1) for n in NAMES}
    eligible = original - {("appendix", 1)}
    result["checks"] = {
        "visual_pages": pages("a_admin") == original and pages("a_reader") == original,
        "chunk_pages": pages("b_admin") == original
        and len(result["b_direct"]["hits"]) == 4,
        "corrected_scan": result["correction_terms"]["rope"] == ["ledger"]
        and "ledger" not in result["correction_terms"]["lamps"],
        "revoked_source_hidden": pages("b_reader") == eligible
        and pages("after_reader") == eligible,
        "hybrid_returns_eligible_pages": pages("b_hybrid_reader") == eligible,
        "dense_top_page": bool(result["a_reader"]["hits"])
        and result["a_reader"]["hits"][0]["source"] == "long"
        and result["after_reader"]["hits"][0:1] == result["b_reader"]["hits"][0:1]
        and result["b_reader"]["hits"][0:1]
        == [{"source": "long", "page": 1, "artifact": "document_chunks_v1"}],
        "alias_served": result["alias_after"] == "b"
        and len(samples) > 0
        and all(s == "ok" for _, s in samples),
        "mutation_during_backfill": not result["b_before_mutation"][
            "publication_complete"
        ]
        or result["b_before_mutation"]["vectors"] < 4,
    }
    if mode == "native":
        data = (directory / "two-pages.pdf").read_bytes()
        write(
            {
                "two": {
                    "url": pdf_url + "two-pages.pdf",
                    "filename": "two-pages.pdf",
                    "version": hashlib.sha256(data).hexdigest(),
                    "mime_type": "application/pdf",
                    "tenant": "public",
                }
            }
        )
        query = "Where is the xenolith spectrometer calibration recorded?"
        e = call(
            base + "/ai/v1/embed",
            "POST",
            {
                "model": MODEL,
                "input": [query],
                "dimensions": 768,
                "task_type": "RETRIEVAL_QUERY",
            },
            args.auth,
        )
        if e["model_identity"] != identity:
            raise ValueError("query encoder changed")

        def page_two():
            hits = search(
                "b", reader, hierarchy={}, query_vector=e["data"][0]["embedding"]
            )["hits"]
            return (
                hits
                if any(h["source"] == "two" and h["page"] == 2 for h in hits)
                else None
            )

        result["two_page_probe"] = wait(page_two, "page two of a multipage PDF")
        top = result["two_page_probe"][0]
        result["checks"]["multipage_top_citation"] = (top["source"], top["page"]) == (
            "two",
            2,
        )
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--endpoint",
        required=True,
        help="dedicated authenticated standalone with public inference enabled",
    )
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args()
    args.auth = os.environ["ARCHIVE_ANTFLY_AUTH"]
    args.out.mkdir(parents=True, exist_ok=False)
    directory = args.out / "objects"
    render_seconds = prepare(directory)
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=directory
    )
    with http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler) as web:
        t = threading.Thread(target=web.serve_forever, daemon=True)
        t.start()
        try:
            results = {}
            for mode in ("native", "rendered"):
                results[mode] = run(
                    args, f"http://127.0.0.1:{web.server_port}/", mode, directory
                )
                publish(args.out / (mode + ".json"), results[mode])
        finally:
            web.shutdown()
            t.join()
    publish(
        args.out / "summary.json",
        {
            "model": MODEL,
            "application_render_seconds_all_versions": render_seconds,
            "checks": {k: r["checks"] for k, r in results.items()},
        },
    )
    return int(not all(all(r["checks"].values()) for r in results.values()))


if __name__ == "__main__":
    raise SystemExit(main())
