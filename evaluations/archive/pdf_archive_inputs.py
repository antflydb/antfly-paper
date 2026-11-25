"""Prepare resumable, matched page-image and OCR passage inputs from the pinned OfficeQA archive."""

import argparse
import fcntl
import hashlib
import json
import time
from collections import Counter
from contextlib import closing
from pathlib import Path

import dataset
import pdf_pipeline
import publisher_replay
from corpus import fetch_object
from evidence import page_id
from native_documents import publish
from pdf_vector_compare import CHUNK_POLICY, MODEL, READER, embed_batch


def sha(path):
    return dataset.sha256_file(path)


def bind_ocr_schedule(out, spec, pdfs, legacy_pdfs):
    marker = out / "identity.json"
    receipts = out / "receipts"
    saved = json.loads(marker.read_text()) if marker.exists() else None
    legacy_spec = spec | {"ocr_retry_truncated_tokens": 8192}
    if saved == legacy_spec:
        # Old workers committed one receipt at a time in this inventory order.
        existing = {p.stem for p in receipts.glob("*.json")}
        prefix = {
            hashlib.sha256(Path(r["path"]).name.encode()).hexdigest()
            for r in pdfs[: len(existing)]
        }
        if prefix != existing or legacy_pdfs not in (None, len(existing)):
            raise ValueError("legacy OCR checkpoints differ from the frozen prefix")
        legacy_pdfs = len(existing)
    elif saved is not None and legacy_pdfs is None:
        legacy_pdfs = saved.get("legacy_ocr_pdfs")
    elif saved is None and any(receipts.glob("*.json")):
        raise ValueError("checkpoint identity missing")
    if legacy_pdfs is None:
        legacy_pdfs = 0
    if not 0 <= legacy_pdfs <= len(pdfs):
        raise ValueError("legacy OCR prefix outside inventory")
    scheduled = spec | {"legacy_ocr_pdfs": legacy_pdfs, "ocr_max_tokens": 8192}
    if saved not in (None, scheduled, legacy_spec):
        raise ValueError("checkpoint identity changed")
    publish(marker, scheduled)
    return scheduled


def prepare(args, cache=None):
    import pypdfium2 as pdfium

    metadata = json.loads((args.workload / "documents.json").read_text())
    expected = json.loads((args.workload / "provenance.json").read_text())
    acquisition = args.data / "dataset-manifest.json"
    if sha(acquisition) != expected["acquisition_sha256"]:
        raise ValueError("acquisition and frozen workload differ")
    inventory = json.loads(acquisition.read_text())
    pdfs = [r for r in inventory["files"] if r["path"].endswith(".pdf")]
    if (
        len(pdfs) != dataset.V2.pdf_count
        or sum(r["physical_pages"] for r in pdfs) != 118524
    ):
        raise ValueError("incomplete OfficeQA PDF inventory")
    if "sample" in expected:
        selected = {r["sha256"]: r["pages"] for r in expected["sample"]}
        pdfs = [r for r in pdfs if r["local_sha256"] in selected]
        if selected != {r["local_sha256"]: r["physical_pages"] for r in pdfs}:
            raise ValueError("sample does not match frozen PDF versions")
    if set(metadata) != {Path(r["path"]).name for r in pdfs}:
        raise ValueError("workload and PDF inventories differ")
    original = dataset.load_questions(args.data)
    if len(original) != dataset.V2.question_count or len(
        {r["uid"] for r in original}
    ) != len(original):
        raise ValueError("invalid original question set")
    prompts = json.loads((args.workload / "queries.json").read_text())
    prompts.extend(
        {
            "id": r["uid"],
            "question": r["question"],
            "tenant": "public",
            "phase": "original",
            "filter": {},
        }
        for r in original
        if "sample" not in expected
    )
    texts = list(dict.fromkeys(q["question"] for q in prompts))
    encoded = {}
    identity = None
    query_path = args.out / "queries.json"
    reuse_queries = (
        cache and query_path.exists() and (args.out / "identity.json").is_file()
    )
    # Probe the loaded model before consulting the cross-revision cache.
    if cache:
        first, identity = embed_batch(args.models, texts[:1], "RETRIEVAL_QUERY")
        encoded[texts[0]] = first[0]
        with cache.db:
            cache.seed_vector(identity, "RETRIEVAL_QUERY", texts[0], first[0])
        cache.counts["model_identity_probes"] += 1
    remaining = texts[1:] if cache else texts
    embed = cache.embed if cache else embed_batch
    encode = cache.passages if cache else pdf_pipeline.passage_vectors
    if reuse_queries:
        remaining = []
    for start in range(0, len(remaining), 16):
        batch = remaining[start : start + 16]
        vectors, identity = embed(args.models, batch, "RETRIEVAL_QUERY", identity)
        encoded.update(zip(batch, vectors, strict=True))
    spec = {
        "representations": "pdf-images-to-passages-v2",
        "acquisition_sha256": sha(acquisition),
        "model": MODEL,
        "model_identity": identity,
        "reader": READER,
        "render_dpi": 144,
        "chunk_policy": dict(CHUNK_POLICY, provider="antfly", model=MODEL),
        "dimensions": 768,
        "subject": args.subject,
        "workload_sha256": {
            n: sha(args.workload / n)
            for n in ("documents.json", "queries.json", "judgments.json")
        },
    }
    if "sample" in expected:
        spec["sample"] = expected["sample"]
        spec["workload_sha256"]["public-probes.json"] = sha(
            args.workload / "public-probes.json"
        )
    replay = (
        publisher_replay.connect(args.replay_catalog) if args.replay_catalog else None
    )
    if replay:
        props = dict(replay.execute("SELECT key,value FROM meta"))
        if (
            props.get("text_complete") != "1"
            or props.get("acquisition") != spec["acquisition_sha256"]
            or props.get("model") != publisher_replay.MODEL
        ):
            raise ValueError("publisher catalog is incomplete or mismatched")
        spec["reader"] = publisher_replay.MODEL
        if not args.native_plan:
            if not args.application_reader:
                raise ValueError("publisher replay requires the application reader URL")
            spec["replay_transport"] = "source-page-png-v1"
    if args.refs:
        refs = json.loads(args.refs.read_text())
        if not set(metadata) <= refs.keys():
            raise ValueError("missing source URLs")
        spec["source_transport"] = "versioned-http"
    pdfs.sort(key=lambda r: (r["physical_pages"], r["path"]))
    if replay:
        if args.legacy_ocr_pdfs:
            raise ValueError("OCR retry schedules do not apply to publisher replay")
        marker = args.out / "identity.json"
        if marker.exists() and json.loads(marker.read_text()) != spec:
            raise ValueError("checkpoint identity changed")
        if not marker.exists() and any((args.out / "receipts").glob("*.json")):
            raise ValueError("checkpoint identity missing")
        publish(marker, spec)
    else:
        spec = bind_ocr_schedule(args.out, spec, pdfs, args.legacy_ocr_pdfs)
    if reuse_queries:
        cache.seed_queries(query_path, prompts, identity)
    else:
        for q in prompts:
            q.update(a=encoded[q["question"]], b=encoded[q["question"]])
        publish(query_path, prompts)
    if args.native_plan:
        plan = spec | {
            "native_source_plan": True,
            "counts": {
                "documents": len(pdfs),
                "pages": sum(r["physical_pages"] for r in pdfs),
                "a_rows": sum(
                    r["physical_pages"] * (1 + len(metadata[Path(r["path"]).name]))
                    for r in pdfs
                ),
            },
        }
        publish(args.out / "native-plan.json", plan)
        return
    for g in ("a", "b"):
        (args.out / g / "documents").mkdir(parents=True, exist_ok=True)
    receipts = args.out / "receipts"
    receipts.mkdir(exist_ok=True)
    totals = Counter()
    for i, r in enumerate(pdfs, 1):
        limits = (
            () if replay else (4096, 8192) if i <= spec["legacy_ocr_pdfs"] else (8192,)
        )
        name = Path(r["path"]).name
        key = hashlib.sha256(name.encode()).hexdigest()
        files = {g: args.out / g / "documents" / (key + ".jsonl") for g in ("a", "b")}
        rec = receipts / (key + ".json")
        if rec.exists():
            saved = json.loads(rec.read_text())
            if tuple(saved.get("ocr_token_limits", (4096, 8192))) != limits:
                raise ValueError("receipt OCR policy differs from the frozen schedule")
            if all(
                file.exists() and sha(file) == saved["sha256"][g]
                for g, file in files.items()
            ):
                if cache:
                    cache.seed_passages(files["b"], saved["sha256"]["b"], identity)
                totals.update(saved["counts"])
                continue
        source = args.data / r["path"]
        if not source.is_relative_to(args.data) or (
            not args.refs and sha(source) != r["local_sha256"]
        ):
            raise ValueError("changed PDF " + key)
        counts = Counter()
        cache_before = cache.counts.copy() if cache else None
        truncated = []
        started = time.monotonic()
        if args.refs:
            ref = refs[name]
            source = fetch_object(ref["url"], r["local_sha256"], ref["etag"])
            counts["bytes_fetched"] = len(source)
        tmp = {g: file.with_suffix(".tmp") for g, file in files.items()}
        with (
            closing(pdfium.PdfDocument(source)) as pdf,
            tmp["a"].open("w") as a,
            tmp["b"].open("w") as b,
        ):
            if len(pdf) != r["physical_pages"]:
                raise ValueError("physical page count mismatch")

            for index in range(len(pdf)):
                with closing(pdf[index]) as page:
                    pid = page_id(name, index + 1)
                    row = {"id": pid, "page": pid, "views": metadata[name]}
                    vector = pdf_pipeline.visual(
                        page, args.models, identity, counts, embed=embed
                    )
                    a.write(
                        json.dumps(
                            row | {"body": "", "a": vector}, separators=(",", ":")
                        )
                        + "\n"
                    )
                    counts["pages"] += 1
                    output = pdf_pipeline.extract(
                        page,
                        index + 1,
                        args.models,
                        limits,
                        replay,
                        r["local_sha256"],
                        counts,
                        args.application_reader,
                    )
                    counts[output["extraction"] + "_pages"] += 1
                    if output["extraction"] == "ocr_truncated":
                        truncated.append(index + 1)
                    if not output["body"].strip():
                        counts["empty_text_pages"] += 1
                    for passage in pdf_pipeline.passages(
                        row,
                        output["body"],
                        args.models,
                        identity,
                        encode=encode,
                    ):
                        b.write(json.dumps(passage, separators=(",", ":")) + "\n")
                        counts["chunks"] += 1
        for g, file in files.items():
            tmp[g].replace(file)
        counts["a_rows"] = counts["pages"] * (1 + len(metadata[name]))
        counts["b_rows"] = counts["chunks"] * (1 + len(metadata[name]))
        counts["documents"] = 1
        record = {
            "ocr_token_limits": limits,
            "counts": dict(counts),
            "truncated_pages": truncated,
            "seconds": time.monotonic() - started,
            "sha256": {g: sha(path) for g, path in files.items()},
        }
        if cache:
            record["preparation_cache"] = dict(cache.counts - cache_before)
        publish(rec, record)
        totals.update(counts)
        print(
            f"committed {i}/{len(pdfs)} PDFs, {totals['pages']} pages, {totals['chunks']} chunks",
            flush=True,
        )
    if (
        totals["pages"] != sum(r["physical_pages"] for r in pdfs)
        or totals["documents"] != len(pdfs)
        or not totals["chunks"]
    ):
        raise ValueError("incomplete representation corpus")
    timing = {}
    for path in receipts.glob("*.json"):
        doc = json.loads(path.read_text())
        limits = doc.get("ocr_token_limits", [4096, 8192])
        key = ",".join(map(str, limits)) or "publisher replay"
        group = timing.setdefault(key, Counter())
        for field in (
            "documents",
            "pages",
            "ocr_calls",
            "ocr_wall_ms",
            "ocr_truncated_calls",
            "replay_calls",
            "replay_submitted_pixels",
            "replay_wall_ms",
        ):
            group[field] += doc["counts"].get(field, 0)
        group["processing_seconds"] += doc["seconds"]
    publish(
        args.out / "manifest.json",
        spec | {"counts": dict(totals), "timing_by_ocr_limits": timing},
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for n in ("data", "workload", "out"):
        p.add_argument("--" + n, type=Path, required=True)
    p.add_argument(
        "--legacy-ocr-pdfs",
        type=int,
        help="replay the old 4096 then 8192 policy for this many initial PDFs in frozen order",
    )
    p.add_argument(
        "--embedding-cache", type=Path, help="persistent offline preparation cache"
    )
    p.add_argument("--application-reader")
    p.add_argument("--refs", type=Path)
    p.add_argument(
        "--native-plan",
        action="store_true",
        help="write native source counts and query vectors without preparing shared rows",
    )
    p.add_argument("--replay-catalog", type=Path)
    p.add_argument("--models", required=True)
    p.add_argument("--subject", required=True, help="runtime binary SHA-256")
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        from preparation_cache import open_cache

        with open_cache(args.embedding_cache, args.subject) as cache:
            prepare(args, cache)
            if cache:
                publish(args.out / "last-cache-run.json", dict(cache.counts))
