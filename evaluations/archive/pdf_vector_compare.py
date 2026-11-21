"""Prepare shared PDF image and passage vectors, then qualify one archive adapter."""

import argparse
import base64
import hashlib
import json
import math
import threading
import uuid
from collections import Counter
from pathlib import Path

from compare import observations
from engines import Antfly, Lance, OpenSearch, Pg, Vespa, call
from pdf_vector_migration import MODEL, NAMES, QUESTION, prepare, wait

READER = (
    "lightonai/LightOnOCR-3-0.8B:safetensors@36d8636925327c671bd8e479c517a004f7566bd9"
)
PAGE_TWO = "Where is the xenolith spectrometer calibration recorded?"


def embed_batch(endpoint, inputs, task, identity=None):
    response = call(
        endpoint.rstrip("/") + "/ai/v1/embed",
        "POST",
        {"model": MODEL, "task_type": task, "dimensions": 768, "input": inputs},
    )
    observed = response["model_identity"]
    if identity is not None and identity != observed:
        raise ValueError("model identity changed during preparation")
    rows = sorted(response["data"], key=lambda row: row["index"])
    if [r["index"] for r in rows] != list(range(len(inputs))):
        raise ValueError("embedding cardinality or indexes differ")
    vectors = [r["embedding"] for r in rows]
    if any(len(v) != 768 or not all(math.isfinite(x) for x in v) for v in vectors):
        raise ValueError("invalid embedding")
    return vectors, observed


def build_inputs(endpoint, out):
    from common import extract_pdf

    out.mkdir(parents=True, exist_ok=False)
    directory = out / "objects"
    render_seconds = prepare(directory)
    costs = Counter()
    identity = None

    def embed(inputs, task):
        nonlocal identity
        vectors, identity = embed_batch(endpoint, inputs, task, identity)
        return vectors

    def rows(name, version, source, image):
        generation = {"a": [], "b": []}

        def row(page, chunk, body, vec):
            return {
                "id": f"{name}{page}c{chunk}",
                "page": f"{name}.pdf#page={page}",
                "body": body,
                "vec": vec,
                "tenant": "public",
                "review_band": 1,
            }

        if image:
            uri = (
                "data:image/png;base64," + base64.b64encode(image.read_bytes()).decode()
            )
            vec = embed(
                [{"content": [{"type": "image_url", "image_url": {"url": uri}}]}],
                "RETRIEVAL_DOCUMENT",
            )[0]
            generation["a"].append(row(1, 0, "", vec))
        pages = extract_pdf(source.read_bytes(), endpoint, reader=READER, costs=costs)
        for page in pages:
            words = page["body"].split()
            chunks = [" ".join(words[i : i + 160]) for i in range(0, len(words), 160)]
            if not chunks:
                raise ValueError(f"empty fixture page: {name} {version} {page['page']}")
            vectors = embed(chunks, "RETRIEVAL_DOCUMENT")
            for i, (chunk, v) in enumerate(zip(chunks, vectors, strict=True)):
                generation["b"].append(row(page["page"], i, chunk, v))
        return generation

    initial = {
        name: rows(name, 1, directory / f"v1/{name}.pdf", directory / f"v1/{name}.png")
        for name in NAMES
    }
    corrected = rows(
        "ledger", 2, directory / "v2/ledger.pdf", directory / "v2/ledger.png"
    )
    extra = rows("two", 1, directory / "two-pages.pdf", None)
    queries = embed([QUESTION, PAGE_TWO], "RETRIEVAL_QUERY")
    payload = {
        "model": MODEL,
        "identity": identity,
        "reader": READER,
        "initial": initial,
        "corrected": corrected,
        "extra": extra,
        "queries": queries,
        "preparation": {
            "application_render_seconds_all_versions": render_seconds,
            "ocr": dict(costs),
            "chunker": "160 words per physical page, zero overlap",
            "image_dpi": 144,
        },
    }
    (out / "inputs.json").write_text(json.dumps(payload, separators=(",", ":")))


def replace_rows(engine, rows):
    # The fixture replaces one existing row per source in each generation.
    if isinstance(engine, Pg):
        with engine.writer.transaction(), engine.writer.cursor() as cur:
            for r in rows:
                cur.execute(
                    f"UPDATE {engine.name} SET tenant=%s,body=%s,vec=%s::vector WHERE id=%s",
                    (r["tenant"], r["body"], str(r["vec"]), r["id"]),
                )
                if cur.rowcount != 1:
                    raise ValueError("missing row to replace")
    elif isinstance(engine, Lance):
        import pyarrow as pa

        batch = pa.Table.from_pylist(rows, schema=engine.table.schema)
        engine.table.merge_insert("id").when_matched_update_all().execute(batch)
    else:
        engine.insert(rows)


def replay(engine_name, endpoint, path):
    raw = path.read_bytes()
    data = json.loads(raw)
    prefix = "pdf" + uuid.uuid4().hex[:8]
    schemas = {}

    def adapter(gen):
        name = prefix + gen
        if engine_name == "antfly":
            return Antfly(endpoint, name, "write", access="api_key")
        if engine_name == "vespa":
            return Vespa(endpoint, name, schemas)
        return {"postgres": Pg, "opensearch": OpenSearch, "lance": Lance}[engine_name](
            endpoint, name
        )

    a, b = adapter("a"), adapter("b")
    qvec, second = data["queries"]

    def query(engine, text=QUESTION, vector=qvec, mode="dense", tenant="public"):
        q = {
            "id": "fixture",
            "question": text,
            "tenant": tenant,
            "filter": {"field": "review_band", "equals": 1},
            "vec": vector,
        }
        return observations(engine, q, "vec", mode, 10, 10)

    initial = data["initial"]
    before = {f"{n}.pdf#page=1" for n in NAMES}
    after = before - {"appendix.pdf#page=1"}

    def phase(engine, g):
        # A reader of the new generation must be able to see every eligible physical page.
        engine.create(768)
        for n in NAMES:
            engine.insert(initial[n][g])
        engine.finish()
        return wait(
            lambda: p if set(p := query(engine)) == before else None,
            g + " initial positive witnesses",
        )

    first = phase(a, "a")
    stop = threading.Event()
    traffic = []

    def probe():
        while not stop.is_set():
            try:
                pages = query(a)
                traffic.append("served" if "long.pdf#page=1" in pages else "miss")
            except (OSError, RuntimeError) as err:
                traffic.append(type(err).__name__)
            stop.wait(0.1)

    t = threading.Thread(target=probe)
    t.start()
    try:
        # Replay the same correction and revocation while B is partially populated.
        b.create(768)
        for n in ("ledger", "appendix"):
            b.insert(initial[n]["b"])
        for engine, g in [(a, "a"), (b, "b")]:
            replacement = data["corrected"][g]
            if [r["id"] for r in replacement] != [
                r["id"] for r in initial["ledger"][g]
            ]:
                raise ValueError("fixture correction changed row keys")
            replace_rows(
                engine,
                replacement + [r | {"tenant": "audit"} for r in initial["appendix"][g]],
            )
        b.insert(initial["long"]["b"])
        a.finish()
        b.finish()

        def settled():
            return all(
                set(query(engine)) == after
                and query(engine, tenant="audit") == ["appendix.pdf#page=1"]
                for engine in (a, b)
            )

        wait(settled, "both generations reflect the revocation")
        lexical = wait(
            lambda: query(b, text="rope", mode="lexical"), "corrected scan visible"
        )
        old_term = query(b, text="lamps", mode="lexical")
        dense = query(b)
        hybrid = query(b, mode="hybrid")
        initial_counts = {"a": a.indexed_counts(), "b": b.indexed_counts()}
        # The shared application routes future reads to B after searchable checks.
        active = b
        post = query(active)
    finally:
        stop.set()
        t.join()
    b.insert(data["extra"]["b"])
    b.finish()
    multi = wait(
        lambda: (
            p
            if "two.pdf#page=2" in (p := query(b, text=PAGE_TWO, vector=second))
            else None
        ),
        "multipage B",
    )
    checks = {
        "visual_top": first[0] == "long.pdf#page=1",
        "passage_top": dense[0] == "long.pdf#page=1",
        "hybrid_top": hybrid[0] == "long.pdf#page=1" and set(hybrid) == after,
        "corrected_scan": lexical == ["ledger.pdf#page=1"]
        and "ledger.pdf#page=1" not in old_term,
        "cutover_scope": post == dense and set(post) == after,
        "live_a": bool(traffic) and all(v == "served" for v in traffic),
        "physical_page_two": multi[0] == "two.pdf#page=2",
    }
    return {
        "engine": engine_name,
        "inputs_sha256": hashlib.sha256(raw).hexdigest(),
        "model_identity": data["identity"],
        "policy": "database"
        if engine_name in ("antfly", "postgres")
        else "application predicate",
        "cutover": "application route",
        "indexed_before_extra_source": initial_counts,
        "a_dense": first,
        "b_dense": dense,
        "b_hybrid": hybrid,
        "b_correction": lexical,
        "b_old_term": old_term,
        "b_multipage": multi,
        "live_a": dict(Counter(traffic)),
        "checks": checks,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    builder = sub.add_parser("prepare")
    builder.add_argument("--models", required=True)
    builder.add_argument("--out", required=True, type=Path)
    run = sub.add_parser("run")
    run.add_argument(
        "engine", choices=("antfly", "lance", "postgres", "opensearch", "vespa")
    )
    run.add_argument("--inputs", required=True, type=Path)
    run.add_argument("--endpoint", required=True)
    run.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        build_inputs(args.models, args.out)
    else:
        r = replay(args.engine, args.endpoint, args.inputs)
        args.out.write_text(json.dumps(r, indent=2) + "\n")
        raise SystemExit(int(not all(r["checks"].values())))
