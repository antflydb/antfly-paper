"""Materialize a public miniature archive with a scan, versioned correction and independent labels."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "head-to-head/scripts"))
from fixture import native_pdf


def materialize(root):
    import pypdfium2 as pdfium

    root.mkdir(parents=True, exist_ok=False)
    rows = [
        (
            "ledger.pdf",
            1,
            "Harbor Bureau paid 40 dollars for lamps in fiscal year 1890.",
            True,
        ),
        (
            "appendix.pdf",
            1,
            "An independent account confirms the Harbor Bureau lamp payment of 40 dollars in fiscal year 1890.",
            False,
        ),
        (
            "ledger.pdf",
            2,
            "Corrected account. Harbor Bureau paid 60 dollars for rope in fiscal year 1891.",
            True,
        ),
    ]
    refs = []
    for name, version, text, scan in rows:
        raw = native_pdf(text)
        folder = root / f"v{version}"
        folder.mkdir(exist_ok=True)
        path = folder / name
        if scan:
            pdf = pdfium.PdfDocument(raw)
            image = pdf[0].render(scale=2).to_pil().convert("RGB")
            # PDF save defaults to the current timestamp. Fix the metadata for byte identity.
            image.save(
                path,
                "PDF",
                resolution=144,
                creationDate="D:20000101000000Z",
                modDate="D:20000101000000Z",
            )
            pdf.close()
        else:
            path.write_bytes(raw)
        refs.append(
            {
                "key": name,
                "version": version,
                "path": str(path.relative_to(root)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "scan": scan,
            }
        )
    (root / "objects.json").write_text(json.dumps(refs, indent=2))
    judgments = {
        "original": [
            ["harbor", "paid-for", "lamps", "ledger.pdf#page=1"],
            ["harbor", "paid-for", "lamps", "appendix.pdf#page=1"],
        ],
        "corrected": [
            ["harbor", "paid-for", "rope", "ledger.pdf#page=1"],
            ["harbor", "paid-for", "lamps", "appendix.pdf#page=1"],
        ],
        "audit_after_revoke": [["harbor", "paid-for", "lamps", "appendix.pdf#page=1"]],
    }
    (root / "graph-gold.json").write_text(json.dumps(judgments, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    materialize(parser.parse_args().directory)
