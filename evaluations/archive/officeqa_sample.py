"""Freeze a stratified whole-PDF sample and page-location probes before inference."""

import argparse
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

import dataset
from corpus import publisher_pages
from evidence import page_id
from native_documents import publish

SEED = "officeqa-v2-raw-pdf-v1"
BOUNDS = (5, 15, 42, 199, 10**9)


def freeze(data, workload, out):
    inventory = dataset.verify_acquisition(data)
    docs = json.loads((workload / "documents.json").read_text())
    strata = defaultdict(list)
    for r in inventory["files"]:
        if r["path"].endswith(".pdf"):
            strata[
                next(i for i, n in enumerate(BOUNDS) if r["physical_pages"] <= n)
            ].append(r)
    queries = []
    judgments = {}
    public = {}
    selected = {}
    plan = []
    for i in range(len(BOUNDS)):
        # Sample complete original objects, independently of runtime and OCR success.
        pool = strata[i]
        r = min(
            pool, key=lambda x: hashlib.sha256((SEED + x["path"]).encode()).digest()
        )
        name = Path(r["path"]).name
        selected[name] = docs[name]
        payload = json.loads(
            (data / dataset.V2.parse_dir / (Path(name).stem + ".json")).read_text()
        )
        tables = {
            box["page_id"] + 1
            for e in payload["document"]["elements"]
            if e["type"] == "table" and any(c.isdigit() for c in e["content"])
            for box in e.get("bbox") or []
        }
        pages = sorted(
            publisher_pages(payload), key=lambda p: (p["page"] not in tables, p["page"])
        )
        for p in pages:
            words = re.findall(
                r"[A-Za-z][A-Za-z'-]+", re.sub(r"<[^>]*>", " ", p["body"])
            )
            if len(words) >= 8:
                break
        else:
            raise ValueError("selected source lacks usable page probe")
        text = " ".join(words[:12])
        pid = page_id(name, p["page"])
        tenant = sorted(docs[name])[0]
        for phase, t in [
            ("initial", tenant),
            ("after-schema-change", tenant),
            ("original", "public"),
        ]:
            key = f"probe-{i}-{phase}"
            filt = (
                {}
                if t == "public"
                else {
                    "field": "retention_years",
                    "equals": docs[name][t]["retention_years"],
                }
            )
            if phase == "initial":
                field = next(f for f in docs[name][t] if f != "retention_years")
                filt = {"field": field, "equals": docs[name][t][field]}
            queries.append(
                {
                    "id": key,
                    "question": text,
                    "phase": phase,
                    "tenant": t,
                    "filter": filt,
                }
            )
            (public if t == "public" else judgments)[key] = {
                "pages": [pid],
                "source": name,
            }
        plan.append(
            {
                "sha256": r["local_sha256"],
                "pages": r["physical_pages"],
                "stratum": i,
                "population_pdfs": len(pool),
                "population_pages": sum(x["physical_pages"] for x in pool),
                "numeric_table_pages": len(tables),
            }
        )
    if not any(r["numeric_table_pages"] for r in plan):
        raise ValueError("missing numeric-table coverage")
    for q in queries:
        g = (public if q["tenant"] == "public" else judgments)[q["id"]]
        g["relevant"] = [g.pop("source")]
        g["eligible"] = [
            n
            for n, v in selected.items()
            if q["tenant"] == "public"
            or v.get(q["tenant"], {}).get(q["filter"]["field"]) == q["filter"]["equals"]
        ]
    out.mkdir(parents=True, exist_ok=True)
    for key, val in [
        ("documents", selected),
        ("queries", queries),
        ("judgments", judgments),
        ("public-probes", public),
        (
            "provenance",
            {
                "acquisition_sha256": dataset.sha256_file(
                    data / "dataset-manifest.json"
                ),
                "sample_seed": SEED,
                "sample": plan,
            },
        ),
    ]:
        f = out / (key + ".json")
        if f.exists() and json.loads(f.read_text()) != val:
            raise ValueError("frozen sample changed")
        publish(f, val)
    print(json.dumps(plan), flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("data", "workload", "out"):
        p.add_argument("--" + key, type=Path, required=True)
    a = p.parse_args()
    freeze(a.data, a.workload, a.out)
