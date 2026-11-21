"""Build shared retrieval inputs. Publisher parses and fresh raw-object runs use distinct directories."""

import argparse
import fcntl
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

import dataset
from evidence import page_id

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "head-to-head/scripts"))
from common import (
    EMBEDDER,
    MIGRATION_EMBEDDER,
    READER,
    digest,
    embed,
    extract_pdf,
    request,
)


def publisher_pages(payload):
    pages = defaultdict(list)
    for element in payload["document"]["elements"]:
        for page in sorted({box["page_id"] for box in element.get("bbox") or []}):
            pages[page + 1].append(element["content"])
    return [
        {"page": n, "body": "\n\n".join(text), "extraction": "publisher"}
        for n, text in sorted(pages.items())
    ]


def fetch_object(url, sha, etag=None):
    headers = {"If-Match": etag} if etag else {}
    with urlopen(Request(url, headers=headers), timeout=180) as response:
        raw = response.read()
        if etag and response.headers.get("ETag") != etag:
            raise ValueError("remote object version mismatch")
    if digest(raw) != sha:
        raise ValueError("remote object content mismatch")
    return raw


def fetch_s3(client, ref, sha):
    args = {
        "Bucket": ref["bucket"],
        "Key": ref["key"],
        "VersionId": ref["version"],
        "IfMatch": ref["etag"],
    }
    response = client.get_object(**args)
    try:
        if (
            response.get("VersionId") != ref["version"]
            or response.get("ETag") != ref["etag"]
        ):
            raise ValueError("S3 response does not identify requested object version")
        raw = response["Body"].read()
    finally:
        response["Body"].close()
    if digest(raw) != sha:
        raise ValueError("remote object content mismatch")
    return raw


def chunk_rows(root):
    single = root / "chunks.jsonl"
    paths = (
        [single] if single.is_file() else sorted((root / "documents").glob("*.jsonl"))
    )
    if not paths:
        raise ValueError("missing corpus chunks")
    for path in paths:
        with path.open() as stream:
            for line in stream:
                yield json.loads(line)


GEMMA = "google/embeddinggemma-2"


def archive_embed(texts, models, model, *, query=False):
    if model != GEMMA:
        return embed(texts, models, model)
    payload = {
        "model": model,
        "input": texts,
        "dimensions": 768,
        "task_type": "RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT",
    }
    rows = request(models + "/ai/v1/embed", payload)["data"]
    if len(rows) != len(texts) or any(len(r["embedding"]) != 768 for r in rows):
        raise ValueError("incomplete EmbeddingGemma 2 response")
    return [r["embedding"] for r in rows]


def flush(pending, output, models, replacement):
    texts = [row["body"] for row in pending]
    a = embed(texts, models, EMBEDDER)
    b = archive_embed(texts, models, replacement)
    if len(a) != len(texts) or len(b) != len(texts):
        raise RuntimeError("incomplete embedding response")
    for i, row in enumerate(pending):
        row.update(a=a[i], b=b[i])
        output.write(json.dumps(row, separators=(",", ":")) + "\n")
    pending.clear()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("data", "workload", "out"):
        p.add_argument("--" + name, required=True, type=Path)
    p.add_argument("--models", required=True)
    p.add_argument(
        "--objects", help="HTTP base URL serving the immutable corpus PDF objects"
    )
    p.add_argument(
        "--object-manifest",
        type=Path,
        help="JSON map of PDF name to versioned URL and optional strong etag",
    )
    p.add_argument(
        "--replacement", choices=(MIGRATION_EMBEDDER, GEMMA), default=MIGRATION_EMBEDDER
    )
    p.add_argument(
        "--ocr-model",
        default=READER,
        help="Reader selector for raw-object pages requiring OCR",
    )
    p.add_argument("--resume", action="store_true")
    p.add_argument(
        "--weights",
        type=Path,
        required=True,
        help="directory containing both mounted encoder model directories",
    )
    p.add_argument("--s3-endpoint", help="endpoint for versioned S3 source descriptors")
    args = p.parse_args()
    if args.objects and args.object_manifest:
        p.error("choose one object source")
    inventory = dataset.verify_acquisition(args.data)
    metadata = json.loads((args.workload / "documents.json").read_text())
    queries = json.loads((args.workload / "queries.json").read_text())
    original = dataset.load_questions(args.data, spec=dataset.V2)
    queries.extend(
        {
            "id": row["uid"],
            "question": row["question"],
            "tenant": "public",
            "phase": "original",
            "filter": {},
        }
        for row in original
    )
    refs = json.loads(args.object_manifest.read_text()) if args.object_manifest else {}
    args.out.mkdir(parents=True, exist_ok=args.resume)
    # Keep an exclusive writer for the entire run, including final query publication.
    lock = (args.out / ".lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if (args.out / "manifest.json").exists():
        raise ValueError("completed corpus already exists")
    weights = {}
    for name in (EMBEDDER, args.replacement):
        directory = args.weights / name
        if not (directory / "model.safetensors").is_file():
            raise ValueError(f"missing pinned model {name}")
        for pth in sorted(directory.rglob("*")):
            if pth.is_file() and not pth.name.endswith(".antfly-sha256-cache"):
                weights[str(pth.relative_to(args.weights))] = dataset.sha256_file(pth)
    identity = {
        "weights": weights,
        "ocr_model": args.ocr_model if args.object_manifest or args.objects else None,
        "code": dataset.sha256_file(Path(__file__)),
        "objects": hashlib.sha256(json.dumps(refs, sort_keys=True).encode()).hexdigest()
        if args.object_manifest
        else args.objects,
        "acquisition": dataset.sha256_file(args.data / "dataset-manifest.json"),
        "workload": dataset.sha256_file(args.workload / "documents.json"),
    }
    record = args.out / "inputs.json"
    if record.exists() and json.loads(record.read_text()) != identity:
        raise ValueError("resume input identity mismatch")
    record.write_text(json.dumps(identity, indent=2))
    completed = args.out / "checkpoints"
    completed.mkdir(exist_ok=True)
    totals = defaultdict(int)
    s3 = None
    if any("bucket" in ref for ref in refs.values()):
        import boto3

        s3 = boto3.client("s3", endpoint_url=args.s3_endpoint)
    shards = args.out / "documents"
    shards.mkdir(exist_ok=True)
    for item in inventory["files"]:
        if not item["path"].endswith(".pdf"):
            continue
        name = Path(item["path"]).name
        final = shards / (name + ".jsonl")
        checkpoint = completed / (name + ".json")
        if checkpoint.is_file():
            saved = json.loads(checkpoint.read_text())
            if dataset.sha256_file(final) != saved["sha256"]:
                raise ValueError("completed shard changed: " + name)
            for field, value in saved["counts"].items():
                totals[field] += value
            continue
        before = dict(totals)
        if args.objects or args.object_manifest:
            ref = (
                refs[name]
                if args.object_manifest
                else {"url": args.objects.rstrip("/") + "/" + quote(name)}
            )
            raw = (
                fetch_s3(s3, ref, item["local_sha256"])
                if "bucket" in ref
                else fetch_object(ref["url"], item["local_sha256"], ref.get("etag"))
            )
            totals["bytes_fetched"] += len(raw)
            pages = extract_pdf(raw, args.models, reader=args.ocr_model, costs=totals)
        else:
            parsed = args.data / dataset.V2.parse_dir / (Path(name).stem + ".json")
            pages = publisher_pages(json.loads(parsed.read_text()))
        pending = []
        partial = final.with_suffix(".partial")
        with partial.open("w") as output:
            for page in pages:
                totals[page["extraction"] + "_pages"] += 1
                words = page["body"].split()
                key = page_id(name, page["page"])
                for i in range(0, len(words), 160):
                    pending.append(
                        dict(
                            id=f"{key}:{i // 160}",
                            page=key,
                            body=" ".join(words[i : i + 160]),
                            views=metadata[name],
                        )
                    )
                    totals["chunks"] += 1
                    if len(pending) == 32:
                        flush(pending, output, args.models, args.replacement)
            if pending:
                flush(pending, output, args.models, args.replacement)
        totals["documents"] += 1
        partial.replace(final)
        stats = {
            "sha256": dataset.sha256_file(final),
            "counts": {k: v - before.get(k, 0) for k, v in totals.items()},
        }
        tmp = checkpoint.with_suffix(".tmp")
        tmp.write_text(json.dumps(stats))
        tmp.replace(checkpoint)
        print(json.dumps(dict(totals)), flush=True)
    cache = {}
    for query in queries:
        text = query["question"]
        if text not in cache:
            cache[text] = {
                g: archive_embed([text], args.models, model, query=True)[0]
                for g, model in (("a", EMBEDDER), ("b", args.replacement))
            }
        query.update(cache[text])
    (args.out / "queries.json").write_text(json.dumps(queries))
    (args.out / "manifest.json").write_text(
        json.dumps(
            dict(
                totals,
                revision=dataset.V2.revision,
                ocr_model=identity["ocr_model"],
                chunker={
                    "unit": "whitespace_words",
                    "size": 160,
                    "overlap": 0,
                    "scope": "page",
                },
                embedding_a=EMBEDDER,
                embedding_b=args.replacement,
                acquisition_sha256=dataset.sha256_file(
                    args.data / "dataset-manifest.json"
                ),
                source="raw-object"
                if args.objects or args.object_manifest
                else "publisher",
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
