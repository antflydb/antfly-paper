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


class HTTPFailure(OSError):
    def __init__(self, response, detail):
        path = urllib.parse.urlparse(response.url).path
        preview = detail[:1000].decode(errors="replace")
        super().__init__(f"HTTP {response.code} {path}: {preview}")
        self.status = response.code
        self.mutation_outcome = response.headers.get("X-Antfly-Raft-Mutation-Outcome")
        self.detail = detail


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
        with exc:
            detail = exc.read(1001)
        raise HTTPFailure(exc, detail) from exc


def extract_page(page, number, models, *, reader, costs=None, ocr_token_limits=None):
    textpage = page.get_textpage()
    text = textpage.get_text_range()
    textpage.close()
    mode = "native"
    if len("".join(text.split())) < 50:
        image = page.render(scale=150 / 72).to_pil()
        stream = io.BytesIO()
        image.save(stream, "PNG")
        uri = "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()
        body = {"model": reader, "images": [{"url": uri}]}
        for budget in ocr_token_limits or (None,):
            if budget is not None:
                body["max_tokens"] = budget
            start = time.monotonic()
            try:
                output = request(
                    models + "/ai/v1/read",
                    body,
                    timeout=900 if ocr_token_limits else 180,
                )
                break
            except OSError as exc:
                cause = exc.__cause__
                if (
                    ocr_token_limits is None
                    or getattr(cause, "code", None) != 400
                    or '"OUTPUT_TRUNCATED"' not in str(exc)
                ):
                    raise
                if costs is not None:
                    costs["ocr_truncated_calls"] += 1
            finally:
                if costs is not None:
                    costs["ocr_calls"] += 1
                    costs["ocr_wall_ms"] += round((time.monotonic() - start) * 1000)
                    costs["ocr_submitted_pixels"] += image.width * image.height
        else:
            return {"page": number, "body": text.strip(), "extraction": "ocr_truncated"}
        if costs is not None:
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
    return {"page": number, "body": text.strip(), "extraction": mode}


def extract_pdf(raw, models, *, reader=READER, costs=None):
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(raw)
    pages = []
    for number, page in enumerate(pdf):
        pages.append(extract_page(page, number + 1, models, reader=reader, costs=costs))
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
