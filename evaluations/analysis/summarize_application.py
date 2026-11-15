"""Verify application instruments/results before describing evidence or code size."""

import argparse
import ast
import hashlib
import io
import json
import subprocess
import tokenize
from pathlib import Path


def sha(data):
    return hashlib.sha256(data).hexdigest()


def committed(revision, path):
    return subprocess.check_output(["git", "show", f"{revision}:{path}"])


def code_lines(source, selected_class=None):
    tree = ast.parse(source)
    selected = (
        next(
            (
                node
                for node in tree.body
                if isinstance(node, ast.ClassDef) and node.name == selected_class
            ),
            None,
        )
        if selected_class
        else None
    )
    if selected_class and selected is None:
        raise ValueError("counting class missing")
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(body, list)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstrings.update(range(body[0].lineno, body[0].end_lineno + 1))
    lines = set()
    ignored = {
        tokenize.COMMENT,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENDMARKER,
        tokenize.ENCODING,
    }
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in ignored:
            continue
        for line in range(token.start[0], token.end[0] + 1):
            if line in docstrings or (
                selected and not selected.lineno <= line <= selected.end_lineno
            ):
                continue
            lines.add(line)
    return len(lines)


def verify(directory, engine, revision):
    receipt = json.loads((directory / "receipt.json").read_text())["sha256"]
    for name, expected in receipt.items():
        if Path(name).name != name or sha((directory / name).read_bytes()) != expected:
            raise ValueError("result checksum mismatch")
    for required in ("result.json", "provenance.json", "model-files.json"):
        if required not in receipt:
            raise ValueError("required evidence is not covered by receipt")
    result = json.loads((directory / "result.json").read_text())
    provenance = json.loads((directory / "provenance.json").read_text())
    if result["engine"] != engine or not result["source_unchanged"]:
        raise ValueError("wrong engine or changed instruments")
    for path, expected in result["source_hashes"].items():
        if sha(committed(revision, path)) != expected:
            raise ValueError(
                "instrument does not match selected committed source: " + path
            )
    return result, provenance


def main(args):
    if len(args.revision) != 40:
        raise ValueError("use the full instrument commit SHA")
    subjects = {}
    for engine, directory in (("antfly", args.antfly), ("postgres", args.postgres)):
        result, provenance = verify(directory, engine, args.revision)
        subjects[engine] = {
            "result": result,
            "provenance": provenance,
            "source_receipt_sha256": sha((directory / "receipt.json").read_bytes()),
        }
    a, p = subjects["antfly"], subjects["postgres"]
    protocol = a["result"].get("protocol")
    if protocol not in {"document-v1", "document-main-v2"} or protocol != p[
        "result"
    ].get("protocol"):
        raise ValueError("unmatched or unknown application protocol")
    for subject in subjects.values():
        if subject["provenance"].get("protocol") != protocol:
            raise ValueError("result/provenance protocol mismatch")
    if a["provenance"].get("subject") != p["provenance"].get("subject"):
        raise ValueError("unmatched candidate subject")
    for field in (
        "fixture_annotations_sha256",
        "model_files_sha256",
        "client_image",
        "subject_image",
        "caps",
        "arguments",
    ):
        if a["provenance"][field] != p["provenance"][field]:
            raise ValueError("unmatched comparison: " + field)
    prefix = "evaluations/head-to-head/scripts/"
    counts = {
        "shared_frontend_and_model_access": sum(
            code_lines(committed(args.revision, prefix + path).decode())
            for path in ("common.py", "frontend.py")
        ),
        "antfly_application_adapter": code_lines(
            committed(args.revision, prefix + "adapters.py").decode(), "Antfly"
        ),
        "postgres_application_adapter": code_lines(
            committed(args.revision, prefix + "adapters.py").decode(), "Postgres"
        ),
        "postgres_outbox_privacy_publication_authentication": code_lines(
            committed(args.revision, prefix + "pipeline.py").decode()
        ),
    }
    qualified = all(
        s["result"]["status"] == "completed"
        and s["result"]["checks"]
        and all(c["passed"] for c in s["result"]["checks"])
        for s in subjects.values()
    )
    timing_eligible = qualified and all(
        s["result"]["fresh_pdf_frontend"]
        and len(s["result"]["phases"]) == 2
        and all(
            phase["summary"]["arrivals"] > 0
            and phase["summary"]["errors"]
            == phase["summary"]["unavailable"]
            == phase["summary"]["unauthorized"]
            == phase["summary"]["email_like_outputs"]
            == 0
            and phase["summary"]["relevant_top1"] == phase["summary"]["arrivals"]
            for phase in s["result"]["phases"]
        )
        for s in subjects.values()
    )
    if protocol == "document-main-v2":
        timing_eligible = timing_eligible and all(
            {phase["name"] for phase in subject["result"]["phases"]}
            == {"serving-only", "serving-with-backfill"}
            and {"scaled-ingest-complete-counts", "backfill-complete-counts"}.issubset(
                c["name"] for c in subject["result"]["checks"] if c["passed"]
            )
            and all(
                phase.get("completed_target_documents", 0) > 0
                and 0
                < phase.get("backfill_completion_seconds", 0)
                <= phase.get("window_and_completion_seconds", 0)
                for phase in subject["result"]["phases"]
                if phase["name"] == "serving-with-backfill"
            )
            for subject in subjects.values()
        )
    report = {
        "protocol": protocol,
        "instrument_commit": args.revision,
        "behavior_qualified_both": qualified,
        "timing_comparison_eligible": timing_eligible,
        "subjects": subjects,
        "code_lines": counts,
        "counting": "physical lines containing Python code tokens, excluding comments/docstrings; engine code, generated code and measurement instruments excluded; LOC is not development effort",
        "operator_time": "not measured; deliberate injected restarts are not manual repair effort",
    }
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    lines = [
        "# Controlled document application evidence",
        "",
        f"Instrument: `{args.revision}`.",
        "",
        f"Both behavior-qualified: **{qualified}**. Timing comparison eligible: **{timing_eligible}**.",
        "",
    ]
    for engine, subject in subjects.items():
        result = subject["result"]
        lines.append(
            f"- {engine}: {result['status']}; {sum(c['passed'] for c in result['checks'])}/{len(result['checks'])} checks passed; fresh PDF frontend: {result['fresh_pdf_frontend']}."
        )
    lines += ["", "Code token lines (not development effort):", ""] + [
        f"- {key}: {value}" for key, value in counts.items()
    ]
    lines += [
        "",
        "No operator-time study was performed. Failure or missing work cannot become a latency win. Raw source/results and their not-run lists constrain every claim.",
    ]
    (args.out / "report.md").write_text("\n".join(lines) + "\n")
    (args.out / "receipt.json").write_text(
        json.dumps(
            {
                "sha256": {
                    name: sha((args.out / name).read_bytes())
                    for name in ("result.json", "report.md")
                }
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--antfly", type=Path, required=True)
    parser.add_argument("--postgres", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--out", type=Path, required=True)
    main(parser.parse_args())
