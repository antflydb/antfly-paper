"""Render the frozen graph sample and blank independent review forms locally."""

import argparse
import hashlib
import json
from contextlib import closing
from pathlib import Path

import dataset
from annotate import sample


def main():
    import pypdfium2 as pdfium

    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("data", "sample", "out"):
        parser.add_argument("--" + key, type=Path, required=True)
    args = parser.parse_args()
    frozen = json.loads(args.sample.read_text())
    inventory = (args.data / "dataset-manifest.json").read_bytes()
    if hashlib.sha256(inventory).hexdigest() != frozen["inventory_sha256"]:
        raise ValueError("sample belongs to a different acquisition manifest")
    pages = frozen["pages"]
    if pages != sample(json.loads(inventory), len(pages), frozen["seed"]):
        raise ValueError("sample differs from its frozen page selection")
    args.out.mkdir(mode=0o700, parents=True, exist_ok=False)
    index = {}
    for i, key in enumerate(pages, 1):
        name, number = key.rsplit("#page=", 1)
        path = args.data / dataset.V2.pdf_dir / name
        with closing(pdfium.PdfDocument(path)) as pdf:
            page = pdf[int(number) - 1]
            try:
                image = f"page-{i:03}.png"
                page.render(scale=2).to_pil().save(args.out / image)
                index[image] = key
            finally:
                page.close()
    (args.out / "index.json").write_text(json.dumps(index, indent=2))
    # Null means unread. The scorer rejects these until the reviewer supplies lists.
    for reviewer in ("reader-1", "reader-2"):
        (args.out / (reviewer + ".json")).write_text(
            json.dumps(dict.fromkeys(pages), indent=2)
        )
    (args.out / "README.txt").write_text(
        "Work independently, using only the page images and index.json. Replace null for "
        "each reviewed page with a list of triples. Include only explicitly supported "
        "[agency, paid-for, item] and [item, fiscal-year, year] facts. Copy names in "
        "lowercase and use four-digit years. Use [] after reviewing a page with no "
        "supported facts. Keep your original forms. After both are finished, reconcile "
        "differences against the images in a separate adjudicated.json and record "
        "reader identities, disagreements and decisions. Keep this entire packet private.\n"
    )


if __name__ == "__main__":
    main()
