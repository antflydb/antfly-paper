"""Shared PDF frontend and model calls; included in both deployed architectures."""

import base64
import hashlib
import hmac
import io
import json
import time
import urllib.error
import urllib.request

EMBEDDER = "BAAI/bge-small-en-v1.5"
MIGRATION_EMBEDDER = "BAAI/bge-small-en"
READER = "antflydb/florence-2-base"
DETECTOR = "fastino/gliner2.5-base-v1-fp16"
DETECTOR_DIRECTORY = "fastino/gliner2.5-base-v1-fp16"


def request(url, body=None, *, method=None, headers=None, timeout=180):
    req = urllib.request.Request(
        url,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
        data=None if body is None else json.dumps(body).encode(),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read(1000).decode(errors="replace")
        raise OSError(
            f"HTTP {exc.code} {urllib.parse.urlparse(url).path}: {detail}"
        ) from exc


def extract_pdf(raw, models, *, reader=READER, costs=None):
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(raw)
    pages = []
    for number, page in enumerate(pdf):
        textpage = page.get_textpage()
        text = textpage.get_text_range()
        textpage.close()
        mode = "native"
        if len("".join(text.split())) < 50:
            image = page.render(scale=150 / 72).to_pil()
            stream = io.BytesIO()
            image.save(stream, "PNG")
            uri = (
                "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()
            )
            start = time.monotonic()
            output = request(
                models + "/ai/v1/read", {"model": reader, "images": [{"url": uri}]}
            )
            if costs is not None:
                costs["ocr_calls"] += 1
                costs["ocr_wall_ms"] += round((time.monotonic() - start) * 1000)
                costs["ocr_submitted_pixels"] += image.width * image.height
                usage = output.get("usage") or {}
                for field in (
                    "prompt_tokens",
                    "completion_tokens",
                    "unmeasured_tokens",
                ):
                    if field in usage:
                        costs["ocr_" + field] += usage[field]
                        costs["ocr_" + field + "_reported_calls"] += 1
            text = output["data"][0]["text"]
            mode = "ocr"
        pages.append({"page": number + 1, "body": text.strip(), "extraction": mode})
        page.close()
    pdf.close()
    return pages


def embed(texts, models, model=EMBEDDER):
    value = request(models + "/ai/v1/embed", {"model": model, "input": texts})
    return [row["embedding"] for row in value["data"]]


def findings(text, models):
    value = request(
        models + "/ai/v1/extract",
        {
            "model": DETECTOR,
            "schema_version": 2,
            "schema": {"entities": ["email"]},
            "options": {"include_spans": True, "offset_unit": "utf8_bytes"},
            "inputs": [{"content": text}],
        },
    )
    return value


def digest(value):
    return hashlib.sha256(value).hexdigest()


def reader_password(master, tenant):
    if tenant not in {"alpha", "beta"}:
        raise ValueError("unknown reader")
    return hmac.new(
        master.encode(), ("document-reader/" + tenant).encode(), hashlib.sha256
    ).hexdigest()
