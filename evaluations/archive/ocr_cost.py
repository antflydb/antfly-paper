"""Summarize the frozen real-OCR sample and estimate shared-worker service time by PDF-length stratum."""

import argparse
import hashlib
import json
from pathlib import Path

from dataset import sha256_file


def summarize(data, workload, inputs):
    plan = json.loads((inputs / "manifest.json").read_text())
    frozen = json.loads((workload / "provenance.json").read_text())
    if plan["sample"] != frozen["sample"] or plan["acquisition_sha256"] != sha256_file(
        data / "dataset-manifest.json"
    ):
        raise ValueError("sample and acquisition differ")
    docs = {
        r["local_sha256"]: Path(r["path"]).name
        for r in json.loads((data / "dataset-manifest.json").read_text())["files"]
        if r["path"].endswith(".pdf")
    }
    rows = []
    for entry in plan["sample"]:
        key = hashlib.sha256(docs[entry["sha256"]].encode()).hexdigest()
        r = json.loads((inputs / "receipts" / (key + ".json")).read_text())
        c = r["counts"]
        if c["pages"] != entry["pages"] or r["ocr_token_limits"] != [8192]:
            raise ValueError("inconsistent sample receipt")
        for gen in ("a", "b"):
            if (
                sha256_file(inputs / gen / "documents" / (key + ".jsonl"))
                != r["sha256"][gen]
            ):
                raise ValueError("changed sample output")
        rows.append(
            {
                **entry,
                "measured_processing_seconds": r["seconds"],
                "measured_ocr_request_seconds": c.get("ocr_wall_ms", 0) / 1000,
                "ocr_calls": c.get("ocr_calls", 0),
                "truncated_pages": c.get("ocr_truncated_pages", 0),
                "reported_prompt_tokens": c.get("ocr_prompt_tokens", 0),
                "reported_completion_tokens": c.get("ocr_completion_tokens", 0),
            }
        )
    return {
        "scope": "shared application pipeline, one serial worker; active completed-PDF time",
        "sample": rows,
        "estimated_full_processing_days": sum(
            r["measured_processing_seconds"] * r["population_pages"] / r["pages"]
            for r in rows
        )
        / 86400,
        "estimated_full_ocr_request_days": sum(
            r["measured_ocr_request_seconds"] * r["population_pages"] / r["pages"]
            for r in rows
        )
        / 86400,
        "method": "page-weighted within PDF-length strata; one whole PDF per stratum; no within-stratum variance estimate",
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for key in ("data", "workload", "inputs"):
        p.add_argument("--" + key, type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(summarize(a.data, a.workload, a.inputs), indent=2))
