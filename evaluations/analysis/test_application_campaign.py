"""Campaign eligibility is scoped and tied to fresh qualification evidence."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import summarize_application_campaign as campaign


class CampaignEligibility(unittest.TestCase):
    def run_campaign(
        self, protocol="paper-application-pairs-v1", changed=False, qualified=True
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = []
            for name in ("qualification-antfly", "qualification-postgres") + tuple(
                f"trial-{n}-{e}" for n in range(1, 4) for e in ("antfly", "postgres")
            ):
                (root / name).mkdir()
                (root / name / "receipt.json").write_text("{}")
                runs.append(dict(name=name, passed=True))
            (root / "campaign.json").write_text(
                json.dumps(
                    dict(
                        source_commit="a" * 40,
                        protocol=protocol,
                        runs=runs,
                        subject_manifest_sha256="fixture",
                    )
                )
            )

            def summarize(args):
                is_qualification = args.antfly.name.startswith("qualification")
                subjects = {}
                for engine in ("antfly", "postgres"):
                    subjects[engine] = dict(
                        provenance=dict(
                            subject="changed"
                            if changed and not is_qualification
                            else "same",
                            arguments=dict(performance=not is_qualification),
                        ),
                        result=dict(
                            phases=[
                                dict(
                                    name=name,
                                    summary=dict(
                                        latency_ms=dict(p50=1, p95=2),
                                        arrivals=1,
                                        errors=0,
                                        unavailable=0,
                                        unauthorized=0,
                                        email_like_outputs=0,
                                        relevant_top1=1,
                                    ),
                                    backfill_completion_seconds=1,
                                    completed_target_documents=1024,
                                )
                                for name in ("serving-only", "serving-with-backfill")
                            ]
                        ),
                    )
                args.out.mkdir()
                (args.out / "result.json").write_text(
                    json.dumps(
                        dict(
                            behavior_qualified_both=qualified,
                            timing_comparison_eligible=True,
                            subjects=subjects,
                        )
                    )
                )

            out = root / "analysis"
            with patch.object(campaign.paired, "main", side_effect=summarize):
                campaign.main(
                    SimpleNamespace(campaign=root, revision="a" * 40, out=out)
                )
            return json.loads((out / "result.json").read_text())

    def test_application_profile_admits_three_verified_pairs(self):
        result = self.run_campaign()
        self.assertTrue(result["comparison_eligible"])
        self.assertEqual(result["summaries"]["antfly"]["serving-only"]["trials"], 3)

    def test_failed_qualification_cannot_be_overridden_by_manifest(self):
        self.assertFalse(self.run_campaign(qualified=False)["comparison_eligible"])

    def test_legacy_campaign_still_requires_component_gates(self):
        result = self.run_campaign(protocol="legacy")
        self.assertFalse(result["comparison_eligible"])
        self.assertIn("native-contracts", result["qualification_gates_not_passed"])

    def test_trial_cannot_change_subject_after_qualification(self):
        with self.assertRaisesRegex(ValueError, "qualification configurations differ"):
            self.run_campaign(changed=True)


if __name__ == "__main__":
    unittest.main()
