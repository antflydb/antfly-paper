"""Authored Apache-2.0 document fixtures and independent acceptance annotations.

These controlled documents test application behavior, not general OCR/relevance.
Run in the locked dependency image to materialize PDFs; no model writes labels.
"""

import argparse
import hashlib
import json
from pathlib import Path

TOPICS = [
    (
        "orchard",
        "Prune apple trees in winter. Remove damaged branches and keep the central canopy open.",
        "When should apple trees be pruned?",
    ),
    (
        "battery",
        "Store lithium batteries in a cool dry cabinet. Inspect swollen cells and isolate damaged packs.",
        "How should lithium batteries be stored?",
    ),
    (
        "backup",
        "Database backups run each night. Restore a snapshot into an isolated environment every Friday.",
        "How frequently should backup restoration be tested?",
    ),
    (
        "water",
        "The water pump needs a clean inlet filter. Shut off power before removing the filter housing.",
        "What must happen before servicing the pump filter?",
    ),
    (
        "telescope",
        "Align the telescope finder in daylight using a distant landmark. Never point the optics at the sun.",
        "How do I align the telescope finder?",
    ),
    (
        "bread",
        "Let bread dough rise until doubled. Bake in a preheated oven and cool the loaf before slicing.",
        "When is bread dough ready to bake?",
    ),
    (
        "bicycle",
        "Check bicycle tire pressure before riding. Replace frayed brake cables and test stopping distance.",
        "What bicycle safety checks should precede a ride?",
    ),
    (
        "seed",
        "Keep tomato seedlings warm and give them bright light. Harden plants outdoors before transplanting.",
        "How should tomato seedlings be prepared for transplanting?",
    ),
]


def documents():
    return [
        {
            "id": f"d{i:03d}",
            "topic": topic,
            "body": text + f" Contact: custodian{i}@example.invalid.",
            "email": f"custodian{i}@example.invalid",
            "query": query,
            "tenant": "alpha" if i % 2 == 0 else "beta",
            "version": 1,
            "scanned": i >= 4,
            "page": 1,
        }
        for i, (topic, text, query) in enumerate(TOPICS)
    ]


def native_pdf(text):
    # Minimal single-page PDF with a native text layer and stable coordinates.
    import textwrap

    lines = textwrap.wrap(text, 68)
    stream = (
        "BT /F1 14 Tf 50 740 Td 20 TL "
        + " ".join(
            ("T* " if i else "")
            + "("
            + line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            + ") Tj"
            for i, line in enumerate(lines)
        )
        + " ET"
    )
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
    ]
    data = b"%PDF-1.4\n"
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{i} 0 obj\n{obj}\nendobj\n".encode()
    start = len(data)
    data += (
        "xref\n0 6\n0000000000 65535 f \n"
        + "".join(f"{n:010d} 00000 n \n" for n in offsets[1:])
        + f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n"
    ).encode()
    return data


def materialize(out):
    import pypdfium2 as pdfium

    out.mkdir(parents=True, exist_ok=False)
    rows = documents()
    for row in rows:
        raw = native_pdf(row["body"])
        path = out / (row["id"] + ".pdf")
        if row["scanned"]:
            pdf = pdfium.PdfDocument(raw)
            image = pdf[0].render(scale=2).to_pil().convert("RGB")
            image.save(path, "PDF", resolution=144.0)
            pdf.close()
        else:
            path.write_bytes(raw)
        row["pdf_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    corrections = []
    for version, text, tenant in [
        (
            2,
            "Orchard maintenance now requires pruning apple trees in late winter, before spring growth.",
            "alpha",
        ),
        (3, "Prune apple trees in late winter. Corrected orchard guidance.", "alpha"),
        (5, "Prune apple trees in late winter. Final orchard guidance.", "beta"),
    ]:
        name = f"d000-v{version}.pdf"
        raw = native_pdf(text)
        (out / name).write_bytes(raw)
        corrections.append(
            {
                "id": "d000",
                "version": version,
                "tenant": tenant,
                "pdf_file": name,
                "pdf_sha256": hashlib.sha256(raw).hexdigest(),
                "body": text,
            }
        )
    (out / "annotations.json").write_text(
        json.dumps(
            {
                "license": "Apache-2.0",
                "scope": "authored controlled qualification fixture",
                "documents": rows,
                "corrections": corrections,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    materialize(parser.parse_args().out)
