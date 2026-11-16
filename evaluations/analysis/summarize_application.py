"""Verify application instruments/results before describing evidence or code size."""

import argparse
import ast
import hashlib
import io
import json
import subprocess
import tokenize
import types
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


V3_CHECKS = {
    "native-and-scanned-routing",
    "qualification-extracted-content",
    "real-detector-span-control",
    "permissioned-semantic-top1",
    "protected-text-and-relevance",
    "protected-embedding-model-boundary",
    "protected-fulltext",
    "committed-artifacts-survive-restart",
    "held-update-converges-and-delete-persists",
    "permission-restriction",
    "migration-new-space-vectors",
    "migration-old-space-still-consistent",
    "rollback-new-a-generation",
}


def verify_v3_oracle(directory, result, revision):
    """Recheck retained persisted values; never invoke inference in analysis."""
    receipt = json.loads((directory / "receipt.json").read_text())["sha256"]
    if "vector-inspections.json" not in receipt:
        raise ValueError("stored-vector evidence missing from receipt")
    payload = (directory / "vector-inspections.json").read_bytes()
    if sha(payload) != receipt["vector-inspections.json"]:
        raise ValueError("stored-vector checksum mismatch")
    records = json.loads(payload)
    attempts = [r for r in records if r.get("kind") == "validation_attempt"]
    if len(attempts) != 3 or {r["index"] for r in attempts} != {"a0", "b1", "a2"}:
        raise ValueError("missing or duplicate vector-space attempts")
    witnesses = {}
    for attempt in attempts:
        before = attempt.get("before_witness")
        if (
            attempt.get("verification") != "accepted"
            or not before
            or before != attempt.get("after_witness")
            or before.get("coverage_complete") is not True
            or before.get("shard_observation_complete") is not True
        ):
            raise ValueError("incomplete or unstable vector-space witness")
        witnesses[attempt["index"]] = before
    oracle = types.ModuleType("frozen_vector_oracle")
    code = committed(revision, "evaluations/head-to-head/scripts/vector_oracle.py")
    exec(compile(code, "frozen/vector_oracle.py", "exec"), oracle.__dict__)
    constants = ast.parse(
        committed(revision, "evaluations/head-to-head/scripts/common.py")
    )
    models = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in constants.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in {"EMBEDDER", "MIGRATION_EMBEDDER"}
    }
    seen = {name: set() for name in witnesses}
    maximum = 0.0
    for record in records:
        if record.get("kind") == "validation_attempt":
            continue
        index, identity = record["index"], record["document_id"]
        if (
            index not in seen
            or identity in seen[index]
            or record.get("verification") != "accepted"
            or record.get("expected_witness") != witnesses[index]
            or record["serving_hit"]["id"] != identity
        ):
            raise ValueError("invalid or duplicate stored-vector receipt")
        model = models["MIGRATION_EMBEDDER" if index == "b1" else "EMBEDDER"]
        if record["expected_model"] != model:
            raise ValueError("wrong expected model")
        expected = record["expected_embedding"]

        def retained_expected(text, record=record, expected=expected):
            if text != record["serving_hit"]["body"]:
                raise ValueError("independent input differs")
            return expected

        oracle.verify_snapshot(
            record["snapshot"],
            record["serving_hit"],
            index,
            model,
            witnesses[index]["policy_version"],
            record["expected_source"],
            retained_expected,
            witnesses[index],
        )
        observed = record["snapshot"]["vectors"][0]["values"]
        maximum = max(maximum, sum((x - y) ** 2 for x, y in zip(observed, expected)))
        seen[index].add(identity)
    if any(ids != {f"d{i:03d}" for i in range(8)} for ids in seen.values()):
        raise ValueError("incomplete fixture document set")
    controls = next(
        c for c in result["checks"] if c["name"] == "vector-oracle-negative-controls"
    ).get("controls", {})
    required_controls = {
        "wrong-model",
        "wrong-vector",
        "missing-vector",
        "unpublished",
        "dirty-checkpoint",
        "replay-debt",
        "wrong-checkpoint-recipe",
        "stale-policy",
        "deleted-identity",
        "stale-source",
    }
    if set(controls) != required_controls or not all(
        v is True for v in controls.values()
    ):
        raise ValueError("negative vector controls missing or failed")
    return {"accepted_vectors": 24, "stable_spaces": 3, "max_squared_error": maximum}


def main(args):
    if len(args.revision) != 40:
        raise ValueError("use the full instrument commit SHA")
    subjects = {}
    for engine, directory in (("antfly", args.antfly), ("postgres", args.postgres)):
        result, provenance = verify(directory, engine, args.revision)
        vector_oracle = None
        if (
            result.get("protocol") == "document-main-v3"
            and result["status"] == "completed"
        ):
            required = V3_CHECKS | (
                {"vector-oracle-negative-controls"} if engine == "antfly" else set()
            )
            names = [c["name"] for c in result["checks"]]
            if len(names) != len(set(names)) or not required <= set(names):
                raise ValueError("missing or duplicate v3 qualification checks")
            if engine == "antfly":
                vector_oracle = verify_v3_oracle(directory, result, args.revision)
        failure = None
        if (directory / "failure.json").exists():
            receipt = json.loads((directory / "receipt.json").read_text())["sha256"]
            if "failure.json" not in receipt:
                raise ValueError("failure observation is not covered by receipt")
            failure = json.loads((directory / "failure.json").read_text())
        subjects[engine] = {
            "failure": failure,
            "vector_oracle": vector_oracle,
            "result": result,
            "provenance": provenance,
            "source_receipt_sha256": sha((directory / "receipt.json").read_bytes()),
        }
    a, p = subjects["antfly"], subjects["postgres"]
    protocol = a["result"].get("protocol")
    if protocol not in {
        "document-v1",
        "document-main-v2",
        "document-main-v3",
    } or protocol != p["result"].get("protocol"):
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
    if protocol in {"document-main-v2", "document-main-v3"}:
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
        "analysis_source_sha256": sha(Path(__file__).read_bytes()),
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
        failure = subject["failure"]
        stopped = (
            f" Stopped at {failure['phase']} ({failure['error']})." if failure else ""
        )
        lines.append(
            f"- {engine}: {result['status']}; {sum(c['passed'] for c in result['checks'])}/{len(result['checks'])} recorded checks passed; fresh PDF frontend: {result['fresh_pdf_frontend']}.{stopped}"
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
