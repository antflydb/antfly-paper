"""Build shared retrieval inputs. Publisher parses and fresh raw-object runs use distinct directories."""
import argparse
import fcntl
import hashlib
from collections import defaultdict
import json
from pathlib import Path
import sys
from urllib.parse import quote
from urllib.request import Request, urlopen

import dataset
from evidence import page_id
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "head-to-head/scripts"))
from common import EMBEDDER, MIGRATION_EMBEDDER, digest, embed, extract_pdf


def publisher_pages(payload):
    pages = defaultdict(list)
    for element in payload["document"]["elements"]:
        for page in sorted({box["page_id"] for box in element.get("bbox") or []}):
            pages[page + 1].append(element["content"])
    return [{"page": n, "body": "\n\n".join(text), "extraction": "publisher"}
            for n, text in sorted(pages.items())]


def fetch_object(url, sha, etag=None):
    headers = {"If-Match": etag} if etag else {}
    with urlopen(Request(url, headers=headers), timeout=180) as response:
        raw = response.read()
        if etag and response.headers.get("ETag") != etag:
            raise ValueError("remote object version mismatch")
    if digest(raw) != sha:
        raise ValueError("remote object content mismatch")
    return raw


def chunk_rows(root):
    single = root / "chunks.jsonl"
    paths = [single] if single.is_file() else sorted((root/"documents").glob("*.jsonl"))
    if not paths:
        raise ValueError("missing corpus chunks")
    for path in paths:
        with path.open() as stream:
            for line in stream:
                yield json.loads(line)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("data", "workload", "out"):
        p.add_argument("--" + name, required=True, type=Path)
    p.add_argument("--models", required=True)
    p.add_argument("--objects", help="HTTP base URL serving the immutable corpus PDF objects")
    p.add_argument("--object-manifest", type=Path, help="JSON map of PDF name to versioned URL and optional strong etag")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--weights",type=Path,required=True,help="directory containing both mounted encoder model directories")
    args = p.parse_args()
    if args.objects and args.object_manifest:
        p.error("choose one object source")
    inventory = dataset.verify_acquisition(args.data)
    metadata = json.loads((args.workload / "documents.json").read_text())
    queries = json.loads((args.workload / "queries.json").read_text())
    original = dataset.load_questions(args.data, spec=dataset.V2)
    queries.extend({"id": row["uid"], "question": row["question"], "tenant": "public",
                    "phase": "original", "filter": {}} for row in original)
    refs = json.loads(args.object_manifest.read_text()) if args.object_manifest else {}
    args.out.mkdir(parents=True, exist_ok=args.resume)
    # Keep an exclusive writer for the entire run, including final query publication.
    lock = (args.out/".lock").open("a")
    fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
    if (args.out/"manifest.json").exists():
        raise ValueError("completed corpus already exists")
    weights={}
    for name in (EMBEDDER,MIGRATION_EMBEDDER):
        directory=args.weights/name
        if not (directory/"model.safetensors").is_file():
            raise ValueError(f"missing pinned model {name}")
        for pth in sorted(directory.rglob("*")):
            if pth.is_file() and not pth.name.endswith(".antfly-sha256-cache"):
                weights[str(pth.relative_to(args.weights))]=dataset.sha256_file(pth)
    identity={"weights":weights,"code":dataset.sha256_file(Path(__file__)),
              "objects":hashlib.sha256(json.dumps(refs,sort_keys=True).encode()).hexdigest() if args.object_manifest else args.objects,
              "acquisition":dataset.sha256_file(args.data/"dataset-manifest.json"),
              "workload":dataset.sha256_file(args.workload/"documents.json")}
    record=args.out/"inputs.json"
    if record.exists() and json.loads(record.read_text())!=identity:
        raise ValueError("resume input identity mismatch")
    record.write_text(json.dumps(identity,indent=2))
    completed=args.out/"checkpoints"
    completed.mkdir(exist_ok=True)
    totals = defaultdict(int)
    shards=args.out/"documents"
    shards.mkdir(exist_ok=True)
    for item in inventory["files"]:
        if not item["path"].endswith(".pdf"):
            continue
        name = Path(item["path"]).name
        final=shards/(name+".jsonl")
        checkpoint=completed/(name+".json")
        if checkpoint.is_file():
            saved=json.loads(checkpoint.read_text())
            if dataset.sha256_file(final)!=saved["sha256"]:
                raise ValueError("completed shard changed: "+name)
            for field,value in saved["counts"].items():
                totals[field]+=value
            continue
        before=dict(totals)
        if args.objects or args.object_manifest:
            ref = refs[name] if args.object_manifest else {"url": args.objects.rstrip("/") + "/" + quote(name)}
            raw = fetch_object(ref["url"], item["local_sha256"], ref.get("etag"))
            totals["bytes_fetched"] += len(raw)
            pages = extract_pdf(raw, args.models)
        else:
            parsed = args.data / dataset.V2.parse_dir / (Path(name).stem + ".json")
            pages = publisher_pages(json.loads(parsed.read_text()))
        pending=[]
        partial=final.with_suffix(".partial")
        with partial.open("w") as output:
            def flush():
                texts=[row["body"] for row in pending]
                a=embed(texts,args.models,EMBEDDER)
                b=embed(texts,args.models,MIGRATION_EMBEDDER)
                if len(a)!=len(texts) or len(b)!=len(texts):
                    raise RuntimeError("incomplete embedding response")
                for i,row in enumerate(pending):
                    row.update(a=a[i],b=b[i])
                    output.write(json.dumps(row,separators=(",",":"))+"\n")
                pending.clear()
            for page in pages:
                totals[page["extraction"]+"_pages"]+=1
                words=page["body"].split()
                key=page_id(name,page["page"])
                for i in range(0,len(words),160):
                    pending.append(dict(id=f"{key}:{i//160}",page=key,body=" ".join(words[i:i+160]),views=metadata[name]))
                    totals["chunks"]+=1
                    if len(pending)==32:
                        flush()
            if pending:
                flush()
        totals["documents"]+=1
        partial.replace(final)
        stats={"sha256":dataset.sha256_file(final),"counts":{k:v-before.get(k,0) for k,v in totals.items()}}
        tmp=checkpoint.with_suffix(".tmp")
        tmp.write_text(json.dumps(stats))
        tmp.replace(checkpoint)
        print(json.dumps(dict(totals)),flush=True)
    cache = {}
    for query in queries:
        text = query["question"]
        if text not in cache:
            cache[text] = {g: embed([text], args.models, model)[0]
                           for g, model in (("a", EMBEDDER), ("b", MIGRATION_EMBEDDER))}
        query.update(cache[text])
    (args.out / "queries.json").write_text(json.dumps(queries))
    (args.out / "manifest.json").write_text(json.dumps(dict(totals, revision=dataset.V2.revision,
        embedding_a=EMBEDDER, embedding_b=MIGRATION_EMBEDDER,
        acquisition_sha256=dataset.sha256_file(args.data / "dataset-manifest.json"),
        source="raw-object" if args.objects or args.object_manifest else "publisher"), indent=2))


if __name__ == "__main__":
    main()
