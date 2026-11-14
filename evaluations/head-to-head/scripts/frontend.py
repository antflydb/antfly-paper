"""Common synchronous PDF acceptance frontend (runs before source acknowledgement).

Its extraction and OCR are deliberately counted on both sides. This experiment
does not claim native privacy covers arbitrary PDF-derived artifact fields.
"""

import argparse
import json
from pathlib import Path

from common import digest, extract_pdf

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    annotations = json.loads((args.data / "annotations.json").read_text())
    rows = []
    for doc in annotations["documents"]:
        raw = (args.data / (doc["id"] + ".pdf")).read_bytes()
        if digest(raw) != doc["pdf_sha256"]:
            raise ValueError("fixture PDF changed")
        pages = extract_pdf(raw, "http://models:8090")
        if len(pages) != 1:
            raise ValueError("qualification fixture expects one page")
        rows.append(
            {
                "id": doc["id"],
                "tenant": doc["tenant"],
                "version": doc["version"],
                **pages[0],
            }
        )
        print(
            json.dumps(
                {
                    "id": doc["id"],
                    "extraction": pages[0]["extraction"],
                    "characters": len(pages[0]["body"]),
                }
            ),
            flush=True,
        )
    args.out.write_text(json.dumps(rows, indent=2) + "\n")
