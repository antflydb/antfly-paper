"""Summarize independent paired trials without pooling request observations."""

import argparse
import json
import statistics
from pathlib import Path
from types import SimpleNamespace

import summarize_application as paired


def main(args):
    campaign = json.loads((args.campaign / "campaign.json").read_text())
    if campaign["source_commit"] != args.revision:
        raise ValueError("campaign instrument commit mismatch")
    args.out.mkdir(parents=True, exist_ok=False)
    qualification_verified = True
    qualification_subjects = None
    if campaign.get("protocol") == "paper-application-pairs-v1":
        qualification = args.out / "qualification"
        paired.main(
            SimpleNamespace(
                antfly=args.campaign / "qualification-antfly",
                postgres=args.campaign / "qualification-postgres",
                revision=args.revision,
                out=qualification,
            )
        )
        qualification_result = json.loads((qualification / "result.json").read_text())
        qualification_verified = qualification_result["behavior_qualified_both"]
        qualification_subjects = qualification_result["subjects"]
    pairs = []
    missing = []
    reference = None
    for trial in range(1, 4):
        paths = {
            e: args.campaign / f"trial-{trial}-{e}" for e in ("antfly", "postgres")
        }
        if not all((p / "receipt.json").exists() for p in paths.values()):
            missing.append(trial)
            continue
        output = args.out / f"pair-{trial}"
        paired.main(SimpleNamespace(**paths, revision=args.revision, out=output))
        result = json.loads((output / "result.json").read_text())
        identity = {
            e: {
                key: result["subjects"][e]["provenance"].get(key)
                for key in (
                    "protocol",
                    "subject",
                    "subject_image",
                    "client_image",
                    "caps",
                    "arguments",
                    "model_files_sha256",
                    "fixture_annotations_sha256",
                )
            }
            for e in paths
        }
        if qualification_subjects is not None:
            for engine, fields in identity.items():
                baseline = qualification_subjects[engine]["provenance"]
                if any(
                    value != baseline.get(key)
                    for key, value in fields.items()
                    if key != "arguments"
                ):
                    raise ValueError("timing and qualification configurations differ")
        if reference is not None and identity != reference:
            raise ValueError("trial configurations differ; do not aggregate")
        reference = identity
        pairs.append(result)
    required = {
        "native-contracts",
        "descriptors",
        "extended",
        "core-antfly",
        "qualification-antfly",
        "qualification-postgres",
    }
    if campaign.get("protocol") == "paper-application-pairs-v1":
        required = {"qualification-antfly", "qualification-postgres"}
    passed = {r["name"] for r in campaign["runs"] if r["passed"]}
    eligible = (
        qualification_verified
        and not missing
        and len(pairs) == 3
        and required <= passed
        and all(p["timing_comparison_eligible"] for p in pairs)
    )
    report = {
        "instrument_commit": args.revision,
        "analysis_source_sha256": paired.sha(Path(__file__).read_bytes()),
        "completed_paired_trials": len(pairs),
        "subject_manifest_sha256": campaign["subject_manifest_sha256"],
        "comparison_eligible": eligible,
        "missing_paired_trials": missing,
        "qualification_gates_not_passed": sorted(required - passed),
        "qualification_scope": campaign.get("scope", "full candidate campaign"),
        "runs": campaign["runs"],
        "aggregation": "median and range of three independent trial summaries; requests are not pooled",
        "summaries": {},
    }
    if eligible:
        for engine in ("antfly", "postgres"):
            result = {}
            for phase in ("serving-only", "serving-with-backfill"):
                rows = [
                    next(
                        v
                        for v in p["subjects"][engine]["result"]["phases"]
                        if v["name"] == phase
                    )
                    for p in pairs
                ]
                summary = {"trials": 3}
                for metric in ("p50", "p95"):
                    values = [r["summary"]["latency_ms"][metric] for r in rows]
                    summary[metric + "_ms"] = {
                        "median": statistics.median(values),
                        "min": min(values),
                        "max": max(values),
                        "per_trial": values,
                    }
                for key in (
                    "arrivals",
                    "errors",
                    "unavailable",
                    "unauthorized",
                    "email_like_outputs",
                    "relevant_top1",
                ):
                    summary[key] = sum(r["summary"][key] for r in rows)
                if phase == "serving-with-backfill":
                    values = [r["backfill_completion_seconds"] for r in rows]
                    summary["backfill_seconds"] = {
                        "median": statistics.median(values),
                        "min": min(values),
                        "max": max(values),
                        "per_trial": values,
                    }
                    summary["completed_documents_per_trial"] = [
                        r["completed_target_documents"] for r in rows
                    ]
                result[phase] = summary
            report["summaries"][engine] = result
    (args.out / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    lines = [
        "# Controlled application trials",
        "",
        f"Comparison eligible: **{eligible}**.",
        "",
        f"Completed paired timing trials: {len(pairs)}/3. The protocol plans three independent trials per architecture at fixed offered load and resource caps. Eligible medians and ranges summarize trial statistics, not pooled requests.",
        "",
    ]
    if eligible:
        lines += [
            "| Architecture | Phase | p50 ms, median (range) | p95 ms, median (range) | Errors / arrivals |",
            "| --- | --- | --- | --- | --- |",
        ]
        for engine, phases in report["summaries"].items():
            for name, phase in phases.items():
                q = [phase[k + "_ms"] for k in ("p50", "p95")]
                values = [
                    f"{v['median']:.1f} ({v['min']:.1f}–{v['max']:.1f})" for v in q
                ]
                lines.append(
                    f"| {engine} | {name} | {values[0]} | {values[1]} | {phase['errors']} / {phase['arrivals']} |"
                )
        for engine, phases in report["summaries"].items():
            backfill = phases["serving-with-backfill"]["backfill_seconds"]
            lines += [
                "",
                f"{engine} backfill: median {backfill['median']:.2f} seconds (range {backfill['min']:.2f}–{backfill['max']:.2f}); produced documents per trial: {phases['serving-with-backfill']['completed_documents_per_trial']}.",
                "",
            ]
    else:
        lines += [
            "Comparative timing is not admitted. Retain the raw failures and missing work; merged code or an available adapter is not an executed result."
        ]
    (args.out / "report.md").write_text("\n".join(lines) + "\n")
    (args.out / "receipt.json").write_text(
        json.dumps(
            {
                "sha256": {
                    n: paired.sha((args.out / n).read_bytes())
                    for n in ("result.json", "report.md")
                }
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--campaign", type=Path, required=True)
    p.add_argument("--revision", required=True)
    p.add_argument("--out", type=Path, required=True)
    main(p.parse_args())
