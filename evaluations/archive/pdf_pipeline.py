"""Shared application pipeline for recomputing either PDF representation during an index build."""

import base64
import io
import json
from collections import Counter
from contextlib import closing

import publisher_replay
from corpus import fetch_object
from evidence import page_id
from pdf_vector_compare import READER, embed_batch


def visual(page, models, identity, costs):
    with closing(page.render(scale=2)) as bitmap:
        with bitmap.to_pil() as image, io.BytesIO() as stream:
            image.save(stream, "PNG")
            encoded = base64.b64encode(stream.getvalue()).decode()
            costs["visual_pixels"] += image.width * image.height
    item = {
        "content": [
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64," + encoded},
            }
        ]
    }
    vectors, _ = embed_batch(models, [item], "RETRIEVAL_DOCUMENT", identity)
    return vectors[0]


def extract(page, number, models, limits, catalog, sha, costs, application_reader=None):
    from common import extract_page

    if catalog is None:
        return extract_page(
            page, number, models, reader=READER, costs=costs, ocr_token_limits=limits
        )
    if not application_reader:
        raise ValueError("missing application replay endpoint")
    measured = Counter()
    result = extract_page(
        page,
        number,
        f"{application_reader}/pages/{sha}/{number}",
        reader=publisher_replay.MODEL,
        costs=measured,
    )
    if result["extraction"] == "native":
        return result
    for k, v in measured.items():
        costs[k.replace("ocr_", "replay_")] += v
    # An empty response can be a known missing page or a mapped empty string.
    missing = (
        not result["body"] and publisher_replay.page_text(catalog, sha, number) is None
    )
    result["extraction"] = "publisher_missing" if missing else "publisher"
    return result


def passages(row, body):
    words = body.split()
    for offset in range(0, len(words), 160):
        yield row | {
            "id": row["page"] + "#chunk=" + str(offset // 160),
            "body": " ".join(words[offset : offset + 160]),
        }


def embedded(rows, models, identity):
    if rows:
        vectors, _ = embed_batch(
            models, [r["body"] for r in rows], "RETRIEVAL_DOCUMENT", identity
        )
        for r, v in zip(rows, vectors, strict=True):
            yield r | {"b": v}


def live_rows(
    gen,
    models,
    refs_path,
    catalog_path,
    manifest,
    workload,
    costs,
    application_reader=None,
):
    import pypdfium2 as pdfium

    meta = json.loads((workload / "documents.json").read_text())
    refs = json.loads(refs_path.read_text())
    catalog = publisher_replay.connect(catalog_path) if catalog_path else None
    if bool(catalog) != (manifest["reader"] == publisher_replay.MODEL):
        raise ValueError("live reader differs from reference inputs")
    if catalog:
        props = dict(catalog.execute("SELECT key,value FROM meta"))
        if (
            props.get("acquisition") != manifest["acquisition_sha256"]
            or props.get("text_complete") != "1"
        ):
            raise ValueError("unexpected replay catalog")
    try:
        for name in sorted(meta):
            ref = refs[name]
            sha = ref["etag"].strip('"')
            if ref["url"].rsplit("/", 1)[-1] != sha or len(sha) != 64:
                raise ValueError("expected content-addressed PDF")
            raw = fetch_object(ref["url"], sha, ref["etag"])
            costs["bytes_fetched"] += len(raw)
            pending = []
            with closing(pdfium.PdfDocument(raw)) as pdf:
                for n in range(1, len(pdf) + 1):
                    with closing(pdf[n - 1]) as page:
                        key = page_id(name, n)
                        row = {"id": key, "page": key, "views": meta[name]}
                        costs["pages"] += 1
                        if gen == "a":
                            yield row | {
                                "body": "",
                                "a": visual(
                                    page, models, manifest["model_identity"], costs
                                ),
                            }
                            continue
                        result = extract(
                            page,
                            n,
                            models,
                            (8192,),
                            catalog,
                            sha,
                            costs,
                            application_reader,
                        )
                        costs[result["extraction"] + "_pages"] += 1
                        for r in passages(row, result["body"]):
                            pending.append(r)
                            if len(pending) == 16:
                                yield from embedded(
                                    pending, models, manifest["model_identity"]
                                )
                                pending.clear()
                yield from embedded(pending, models, manifest["model_identity"])
    finally:
        if catalog:
            catalog.close()
