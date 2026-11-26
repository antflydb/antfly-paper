"""Print a source inventory for the two full PDF deployments, grouped by responsibility."""

import ast
import sys
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "analysis"))
from summarize_application import code_lines  # noqa: E402

# Count shared functions separately even when their callers are engine specific.
GROUPS = {
    "Native Antfly": {
        "Reader, indexing and tenant setup": (
            "engines.py",
            "Antfly.__init__ Antfly.schema Antfly.create_readers Antfly.api_key ApiKey",
        ),
        "Index definitions": ("pdf_vector_migration.py", "index_config"),
        "Scoped search and physical page mapping": (
            "pdf_native_archive.py",
            "PageSearch",
        ),
        "Ingestion, alias, readiness and measurement (mixed)": (
            "pdf_native_archive.py",
            "run",
        ),
    },
    "Application-managed PostgreSQL": {
        "Database, RLS, writes and search": ("engines.py", "Pg"),
        "PDF producer including imports": ("pdf_pipeline.py", ""),
        "Image embedding and passage pagination": (
            "pdf_vector_compare.py",
            "embed_batch passage_vectors",
        ),
        "Versioned PDF fetch": ("corpus.py", "fetch_object"),
        "PDF extraction, fallback and HTTP helpers": (
            "../head-to-head/scripts/common.py",
            "pdf_text_layer extract_page request digest",
        ),
        "Tenant expansion and batched loader": ("compare.py", "load"),
        "Client routing, readiness and measurement (mixed)": (
            "compare.py",
            "main wait_for_indexed_rows",
        ),
    },
    "Shared infrastructure and measurement": {
        "HTTP and filter helpers": ("engines.py", "call filters"),
        "Object endpoint": ("local_objects.py", ""),
        "Publisher replay service and catalog tools": ("publisher_replay.py", ""),
        "Deploy and prepare": ("document_study.py", ""),
        "Engine provisioning, receipt checks and scoring orchestration": (
            "pdf_archive_campaign.py",
            "",
        ),
        "Serving schedule, ranking and replay": (
            "compare.py",
            "observations scheduled_reads replay",
        ),
        "Page identities, fusion and citation scoring": (
            "evidence.py",
            "page_id fuse score_pages",
        ),
    },
}


def measure(path, names):
    source = path.read_text()
    if not names:
        return code_lines(source)
    root = ast.parse(source)
    lines = source.splitlines()
    total = 0
    for name in names.split():
        node = root
        for part in name.split("."):
            node = next(n for n in node.body if getattr(n, "name", None) == part)
        start = min(d.lineno for d in [node, *node.decorator_list])
        total += code_lines(
            textwrap.dedent("\n".join(lines[start - 1 : node.end_lineno]))
        )
    return total


if __name__ == "__main__":
    print("| Deployment | Responsibility | Source scope | Code lines |")
    print("|---|---|---|---:|")
    for group, scopes in GROUPS.items():
        for label, (file, names) in scopes.items():
            count = measure(HERE / file, names)
            print(f"| {group} | {label} | `{file}` {names} | {count} |")
