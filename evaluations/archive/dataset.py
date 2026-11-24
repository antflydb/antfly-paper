from __future__ import annotations

import ast
import csv
import hashlib
import json
import os
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def write_json(path, payload):
    path.write_text(json.dumps(payload, indent=2) + "\n")


REWARD_COMMIT = "86753108d69e149cc28abd346bb8c3ca1cbfc7cf"
REWARD_URL = (
    f"https://raw.githubusercontent.com/databricks/officeqa/{REWARD_COMMIT}/reward.py"
)
REWARD_SHA256 = "0d91698c87df6d889339aac36f63ae0966607f169890b0bf8b472b26bfe8138f"


@dataclass(frozen=True)
class DatasetSpec:
    version: str
    repo_id: str
    revision: str | None
    benchmark_id: str
    csv_name: str
    pdf_dir: str
    parse_dir: str
    question_count: int
    pdf_count: int
    primary_metric: str

    def identity(self) -> dict[str, Any]:
        return {
            "dataset_version": self.version,
            "repo_id": self.repo_id,
            "revision": self.revision,
            "benchmark_id": self.benchmark_id,
            "primary_metric": self.primary_metric,
        }


V2 = DatasetSpec(
    "v2",
    "databricks/officeqa-pro-v2",
    "65a2b315780417bc50d7bfe6e5bdb904e63fda65",
    "office-qa-pro-v2",
    "officeqa_pro_v2.csv",
    "pdfs",
    "parsed_corpus/jsons",
    90,
    1435,
    "exact",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_acquisition(data_dir: Path, spec: DatasetSpec = V2) -> dict[str, Any]:
    path = data_dir / "dataset-manifest.json"
    if not path.is_file():
        raise ValueError(
            "verified acquisition manifest is required; run the pinned download first"
        )
    manifest = json.loads(path.read_text())
    if (
        manifest.get("repo_id") != spec.repo_id
        or manifest.get("resolved_revision") != spec.revision
        or manifest.get("raw_pdfs") != spec.pdf_count
        or manifest.get("publisher_jsons") != spec.pdf_count
        or manifest.get("reward_sha256") != REWARD_SHA256
    ):
        raise ValueError(
            "acquisition manifest does not identify the complete pinned V2 corpus and scorer"
        )
    files = manifest.get("files") or []
    expected = {spec.csv_name}
    expected.update(
        path.relative_to(data_dir).as_posix() for path in pdf_paths(data_dir, spec=spec)
    )
    expected.update(
        path.relative_to(data_dir).as_posix()
        for path in transformed_paths(data_dir, spec)
    )
    if (
        len(files) != 2 * spec.pdf_count + 1
        or {item["path"] for item in files} != expected
    ):
        raise ValueError(
            "acquisition inventory does not match the complete local V2 corpus"
        )
    for item in files:
        relative = Path(item["path"])
        path = data_dir / relative
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or path.is_symlink()
            or not path.is_file()
        ):
            raise ValueError("unsafe acquisition manifest object path")
        if (
            path.stat().st_size != item["size"]
            or sha256_file(path) != item["local_sha256"]
        ):
            raise ValueError(f"acquired object changed: {relative}")
    if sha256_file(data_dir / "reward.py") != REWARD_SHA256:
        raise ValueError("acquired scorer digest mismatch")
    return manifest


def load_questions(data_dir: Path, spec: DatasetSpec = V2) -> list[dict[str, Any]]:
    with (data_dir / spec.csv_name).open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    required = {"uid", "question", "answer", "source_files", "source_docs"}
    missing = required - set(rows[0] if rows else {})
    if missing:
        raise ValueError(f"OfficeQA CSV missing fields: {sorted(missing)}")
    for row in rows:
        row["uid"] = str(row["uid"])
    return rows


def questions_by_id(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row["uid"]): row for row in rows}


def pdf_paths(
    data_dir: Path, source_dir: Path | None = None, spec: DatasetSpec = V2
) -> list[Path]:
    root = source_dir or data_dir / spec.pdf_dir
    paths = sorted(root.glob("*.pdf"))
    if not paths:
        raise FileNotFoundError(f"no raw PDFs under {root}")
    return paths


def transformed_paths(data_dir: Path, spec: DatasetSpec = V2) -> list[Path]:
    root = data_dir / spec.parse_dir
    paths = sorted(path for path in root.rglob("*") if path.is_file())
    if not paths:
        raise FileNotFoundError(f"no oracle transformed files under {root}")
    return paths


def parse_source_files(value: Any) -> list[str]:
    text = str(value or "").strip()
    parsed: Any = None
    if text:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            try:
                parsed = ast.literal_eval(text)
            except (ValueError, SyntaxError):
                parsed = [part.strip() for part in re.split(r"[,;\r\n]+", text)]
    if isinstance(parsed, str):
        parsed = [parsed]
    if not isinstance(parsed, (list, tuple, set)):
        raise ValueError(f"unsupported OfficeQA source_files value: {value!r}")
    names: dict[str, str] = {}
    for item in parsed:
        raw = str(item).strip()
        if not raw:
            continue
        path = Path(raw)
        if path.name != raw or path.is_absolute():
            raise ValueError(
                f"OfficeQA source_files must contain PDF basenames: {raw!r}"
            )
        if path.suffix.lower() == ".txt":
            raw = path.with_suffix(".pdf").name
        elif path.suffix.lower() != ".pdf":
            raise ValueError(f"unsupported OfficeQA source extension: {raw!r}")
        identity = raw.casefold()
        prior = names.get(identity)
        if prior is not None and prior != raw:
            raise ValueError(
                f"conflicting OfficeQA PDF identities: {prior!r} and {raw!r}"
            )
        names[identity] = raw
    return sorted(names.values())


def download(data_dir: Path, spec: DatasetSpec = V2) -> dict[str, Any]:
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise RuntimeError(
            f"HF_TOKEN is required with approved access to {spec.repo_id}; access terms are not accepted automatically"
        )
    from huggingface_hub import HfApi, snapshot_download

    previous = data_dir / "dataset-manifest.json"
    if (
        previous.exists()
        and json.loads(previous.read_text()).get("repo_id") != spec.repo_id
    ):
        raise ValueError("refusing to replace another dataset's acquisition manifest")

    resolved = HfApi(token=token).dataset_info(
        spec.repo_id, revision=spec.revision, files_metadata=True
    )
    if spec.revision and resolved.sha != spec.revision:
        raise ValueError("resolved dataset revision differs from pinned revision")
    allow = [spec.csv_name, f"{spec.pdf_dir}/*.pdf", f"{spec.parse_dir}/*.json"]
    snapshot_download(
        repo_id=spec.repo_id,
        repo_type="dataset",
        revision=resolved.sha,
        token=token,
        allow_patterns=allow,
        local_dir=data_dir,
    )
    reward_path = data_dir / "reward.py"
    urllib.request.urlretrieve(REWARD_URL, reward_path)
    reward_sha = sha256_file(reward_path)
    if reward_sha != REWARD_SHA256:
        raise ValueError("pinned reward.py digest mismatch")
    manifest = {
        **spec.identity(),
        "requested_revision": spec.revision,
        "resolved_revision": resolved.sha,
        "reward_commit": REWARD_COMMIT,
        "reward_sha256": reward_sha,
        "oracle_included": True,
    }
    pdfs = pdf_paths(data_dir, spec=spec)
    manifest["raw_pdfs"] = len(pdfs)
    import pypdfium2 as pdfium

    files = []
    inventory = [
        item
        for item in resolved.siblings
        if item.rfilename == spec.csv_name
        or (
            item.rfilename.startswith(spec.pdf_dir + "/")
            and item.rfilename.endswith(".pdf")
        )
        or (
            item.rfilename.startswith(spec.parse_dir + "/")
            and item.rfilename.endswith(".json")
        )
    ]
    upstream_pdfs = {
        item.rfilename for item in inventory if item.rfilename.endswith(".pdf")
    }
    upstream_jsons = {
        item.rfilename for item in inventory if item.rfilename.endswith(".json")
    }
    if len(upstream_pdfs) != spec.pdf_count or len(upstream_jsons) != spec.pdf_count:
        raise ValueError(
            "pinned V2 upstream inventory does not contain 1,435 PDFs and JSONs"
        )
    if {f"{spec.pdf_dir}/{p.name}" for p in pdfs} != upstream_pdfs:
        raise ValueError(
            "local V2 PDF inventory differs from pinned upstream inventory"
        )
    parses = transformed_paths(data_dir, spec)
    if {p.relative_to(data_dir).as_posix() for p in parses} != upstream_jsons:
        raise ValueError(
            "local V2 JSON inventory differs from pinned upstream inventory"
        )
    if {Path(p).stem for p in upstream_pdfs} != {Path(p).stem for p in upstream_jsons}:
        raise ValueError("V2 PDF and JSON identities do not match")
    page_total = 0
    publisher_page_total = 0
    for item in sorted(inventory, key=lambda item: item.rfilename):
        path = data_dir / item.rfilename
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"invalid acquired object {item.rfilename}")
        digest = sha256_file(path)
        lfs = item.lfs
        upstream_sha = (
            (
                lfs.get("sha256")
                if isinstance(lfs, dict)
                else getattr(lfs, "sha256", None)
            )
            if lfs
            else None
        )
        if path.stat().st_size != item.size or (
            upstream_sha and digest != upstream_sha
        ):
            raise ValueError(f"upstream object verification failed: {item.rfilename}")
        if not upstream_sha:
            content = path.read_bytes()
            git_sha = hashlib.sha1(
                f"blob {len(content)}\0".encode() + content
            ).hexdigest()
            if git_sha != item.blob_id:
                raise ValueError(
                    f"upstream Git object verification failed: {item.rfilename}"
                )
        entry = {
            "path": item.rfilename,
            "size": item.size,
            "lfs_sha256": upstream_sha,
            "git_blob_sha": item.blob_id,
            "local_sha256": digest,
        }
        if item.rfilename.endswith(".pdf"):
            reader = pdfium.PdfDocument(path)
            try:
                count = len(reader)
            finally:
                reader.close()
            entry["physical_pages"] = count
            page_total += count
            parsed = json.loads(
                (data_dir / spec.parse_dir / (path.stem + ".json")).read_text()
            )
            validate_publisher_pages(parsed, count)
            entry["publisher_declared_pages"] = len(parsed["document"]["pages"])
            publisher_page_total += entry["publisher_declared_pages"]
        files.append(entry)
    rows = load_questions(data_dir, spec=spec)
    if (
        len(rows) != spec.question_count
        or len(questions_by_id(rows)) != spec.question_count
    ):
        raise ValueError("V2 CSV must contain 90 distinct questions")
    names = {p.name for p in pdfs}
    for row in rows:
        if not set(parse_source_files(row["source_files"])).issubset(names):
            raise ValueError(
                f"source hint references missing V2 original: {row['uid']}"
            )
    manifest.update(
        files=files,
        physical_pages=page_total,
        publisher_declared_pages=publisher_page_total,
        questions=len(rows),
        publisher_jsons=len(parses),
        oracle_representation="document.elements content joined with blank lines; raw HTML preserved",
    )
    write_json(data_dir / "dataset-manifest.json", manifest)
    return manifest


def validate_publisher_pages(payload: dict[str, Any], physical_pages: int) -> None:
    document = payload["document"]
    declared = [page["id"] for page in document["pages"]]
    if any(
        type(page) is not int or not 0 <= page < physical_pages for page in declared
    ):
        raise ValueError("publisher JSON has invalid zero-based declared page IDs")
    if len(set(declared)) != len(declared):
        raise ValueError("publisher JSON has duplicate declared page IDs")
    for element in document["elements"]:
        if not isinstance(element.get("content"), str):
            raise ValueError("publisher JSON element content must be text")
        for box in element.get("bbox") or []:
            page = box.get("page_id")
            if type(page) is not int or page not in declared:
                raise ValueError("publisher JSON element references an unknown page")
