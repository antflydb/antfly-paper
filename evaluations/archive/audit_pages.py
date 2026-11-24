"""Create a private reproducible visual audit packet of publisher citations and physical pages."""

import argparse
import hashlib
import json
from pathlib import Path

import dataset
from corpus import publisher_pages
from evidence import citations


def main():
    import pypdfium2 as pdfium

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--count", type=int, default=16)
    args = p.parse_args()
    inventory = dataset.verify_acquisition(args.data)
    counts = {
        Path(f["path"]).name: f["physical_pages"]
        for f in inventory["files"]
        if f["path"].endswith(".pdf")
    }
    pairs = [
        (r["uid"], doc, n)
        for r in dataset.load_questions(args.data, spec=dataset.V2)
        for doc, n in citations(r, counts)
    ]
    pairs.sort(key=lambda key: hashlib.sha256(json.dumps(key).encode()).digest())
    if not 1 <= args.count <= len(pairs):
        raise ValueError("invalid audit size")
    args.out.mkdir(parents=True, exist_ok=False)
    records = []
    for i, (uid, doc, n) in enumerate(pairs[: args.count]):
        pdf = pdfium.PdfDocument(args.data / dataset.V2.pdf_dir / doc)
        page = pdf[n - 1]
        try:
            page.render(scale=1.5).to_pil().save(args.out / f"{i:03d}.png")
        finally:
            page.close()
            pdf.close()
        parsed = json.loads(
            (args.data / dataset.V2.parse_dir / (Path(doc).stem + ".json")).read_text()
        )
        text = next((r["body"] for r in publisher_pages(parsed) if r["page"] == n), "")
        (args.out / f"{i:03d}.txt").write_text(text)
        records.append(
            {
                "uid": uid,
                "document": doc,
                "physical_page": n,
                "pdfium_index": n - 1,
                "image": f"{i:03d}.png",
                "publisher_text": f"{i:03d}.txt",
                "page_agrees": None,
                "evidence_present": None,
                "reviewer": None,
            }
        )
    (args.out / "audit.json").write_text(json.dumps(records, indent=2))


if __name__ == "__main__":
    main()
