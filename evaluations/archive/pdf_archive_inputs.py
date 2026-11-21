"""Prepare resumable, matched page-image and OCR passage inputs from the pinned OfficeQA archive."""

import argparse
import base64
import fcntl
import hashlib
import io
import json
import time
from collections import Counter
from contextlib import closing
from pathlib import Path

import dataset
from evidence import page_id
from native_documents import publish
from pdf_vector_compare import MODEL, READER, embed_batch


def sha(path):
    return dataset.sha256_file(path)


def flush(pending, stream, models, identity):
    if not pending:
        return
    vectors, _ = embed_batch(
        models,
        [row["body"] for row in pending],
        "RETRIEVAL_DOCUMENT",
        identity,
    )
    for row, vector in zip(pending, vectors, strict=True):
        stream.write(json.dumps(row | {"b": vector}, separators=(",", ":")) + "\n")
    pending.clear()


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


def prepare(args):
    import pypdfium2 as pdfium
    from common import extract_page

    metadata = json.loads((args.workload / "documents.json").read_text())
    expected = json.loads((args.workload / "provenance.json").read_text())
    acquisition = args.data / "dataset-manifest.json"
    if sha(acquisition) != expected["acquisition_sha256"]:
        raise ValueError("acquisition and frozen workload differ")
    inventory = json.loads(acquisition.read_text())
    pdfs = [r for r in inventory["files"] if r["path"].endswith(".pdf")]
    if (
        len(pdfs) != dataset.V2.pdf_count
        or set(metadata) != {Path(r["path"]).name for r in pdfs}
        or sum(r["physical_pages"] for r in pdfs) != 118524
    ):
        raise ValueError("incomplete OfficeQA PDF inventory")
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
    )
    texts = list(dict.fromkeys(q["question"] for q in prompts))
    encoded = {}
    identity = None
    for start in range(0, len(texts), 16):
        batch = texts[start : start + 16]
        vectors, identity = embed_batch(args.models, batch, "RETRIEVAL_QUERY", identity)
        encoded.update(zip(batch, vectors, strict=True))
    spec = {
        "representations": "pdf-images-to-passages-v1",
        "acquisition_sha256": sha(acquisition),
        "model": MODEL,
        "model_identity": identity,
        "reader": READER,
        "render_dpi": 144,
        "chunk_words": 160,
        "dimensions": 768,
        "subject": args.subject,
        "workload_sha256": {
            n: sha(args.workload / n)
            for n in ("documents.json", "queries.json", "judgments.json")
        },
    }
    pdfs.sort(key=lambda r: (r["physical_pages"], r["path"]))
    spec = bind_ocr_schedule(args.out, spec, pdfs, args.legacy_ocr_pdfs)
    for q in prompts:
        q.update(a=encoded[q["question"]], b=encoded[q["question"]])
    publish(args.out / "queries.json", prompts)
    for g in ("a", "b"):
        (args.out / g / "documents").mkdir(parents=True, exist_ok=True)
    receipts = args.out / "receipts"
    receipts.mkdir(exist_ok=True)
    totals = Counter()
    for i, r in enumerate(pdfs, 1):
        limits = (4096, 8192) if i <= spec["legacy_ocr_pdfs"] else (8192,)
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
                totals.update(saved["counts"])
                continue
        source = args.data / r["path"]
        if not source.is_relative_to(args.data) or sha(source) != r["local_sha256"]:
            raise ValueError("changed PDF " + key)
        counts = Counter()
        truncated = []
        started = time.monotonic()
        tmp = {g: file.with_suffix(".tmp") for g, file in files.items()}
        with (
            closing(pdfium.PdfDocument(source)) as pdf,
            tmp["a"].open("w") as a,
            tmp["b"].open("w") as b,
        ):
            if len(pdf) != r["physical_pages"]:
                raise ValueError("physical page count mismatch")
            pending = []

            for index in range(len(pdf)):
                with closing(pdf[index]) as page:
                    pid = page_id(name, index + 1)
                    row = {"id": pid, "page": pid, "views": metadata[name]}
                    with closing(page.render(scale=2)) as bitmap:
                        with bitmap.to_pil() as image, io.BytesIO() as stream:
                            image.save(stream, "PNG")
                            encoded_image = base64.b64encode(stream.getvalue()).decode()
                            counts["visual_pixels"] += image.width * image.height
                    media = {
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/png;base64," + encoded_image
                                },
                            }
                        ]
                    }
                    images, _ = embed_batch(
                        args.models, [media], "RETRIEVAL_DOCUMENT", identity
                    )
                    a.write(
                        json.dumps(
                            row | {"body": "", "a": images[0]}, separators=(",", ":")
                        )
                        + "\n"
                    )
                    counts["pages"] += 1
                    output = extract_page(
                        page,
                        index + 1,
                        args.models,
                        reader=READER,
                        costs=counts,
                        ocr_token_limits=limits,
                    )
                    counts[output["extraction"] + "_pages"] += 1
                    if output["extraction"] == "ocr_truncated":
                        truncated.append(index + 1)
                    words = output["body"].split()
                    if not words:
                        counts["empty_text_pages"] += 1
                    for offset in range(0, len(words), 160):
                        pending.append(
                            row
                            | {
                                "id": pid + "#chunk=" + str(offset // 160),
                                "body": " ".join(words[offset : offset + 160]),
                            }
                        )
                        counts["chunks"] += 1
                        if len(pending) == 16:
                            flush(pending, b, args.models, identity)
            flush(pending, b, args.models, identity)
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
        publish(rec, record)
        totals.update(counts)
        print(
            f"committed {i}/{len(pdfs)} PDFs, {totals['pages']} pages, {totals['chunks']} chunks",
            flush=True,
        )
    if totals["pages"] != 118524 or totals["documents"] != 1435 or not totals["chunks"]:
        raise ValueError("incomplete representation corpus")
    timing = {}
    for path in receipts.glob("*.json"):
        doc = json.loads(path.read_text())
        limits = doc.get("ocr_token_limits", [4096, 8192])
        key = ",".join(map(str, limits))
        group = timing.setdefault(key, Counter())
        for field in (
            "documents",
            "pages",
            "ocr_calls",
            "ocr_wall_ms",
            "ocr_truncated_calls",
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
    p.add_argument("--models", required=True)
    p.add_argument("--subject", required=True, help="runtime binary SHA-256")
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        prepare(args)
