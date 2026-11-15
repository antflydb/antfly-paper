"""Candidate source/image binding must fail closed before mutating services."""

import json
import tempfile
import unittest
from pathlib import Path

from subject_manifest import load_subject, verify_image


class SubjectTests(unittest.TestCase):
    def test_mutable_and_release_masquerading_subjects_rejected(self):
        value = {
            "name": "main-" + "a" * 12,
            "commit": "a" * 40,
            "image": "sha256:" + "b" * 64,
            "binary_sha256": "c" * 64,
            "build_profile": "linux-arm64-gnu-release",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "subject.json"
            path.write_text(json.dumps(value))
            self.assertEqual(load_subject(path, "paper-main-test")["commit"], "a" * 40)
            for protocol, tag, patch in [
                ("paper-default-v030-v5", None, {}),
                ("paper-main-test", "v0.3.0", {}),
                ("paper-main-test", None, {"image": "antfly:main"}),
                ("paper-main-test", None, {"commit": "d" * 40}),
            ]:
                path.write_text(json.dumps({**value, **patch}))
                with self.assertRaises(ValueError):
                    load_subject(path, protocol, tag)

    def test_wrong_image_source_and_executable_are_rejected(self):
        def mismatched(args):
            if args[0] == "image":
                return json.dumps(
                    [
                        {
                            "Id": "sha256:" + "b" * 64,
                            "Config": {
                                "Labels": {
                                    "org.opencontainers.image.revision": "d" * 40
                                }
                            },
                        }
                    ]
                )
            return "e" * 64 + "  /antfly"

        with self.assertRaises(ValueError):
            verify_image("sha256:" + "b" * 64, "a" * 40, "c" * 64, mismatched)
        with self.assertRaises(ValueError):
            verify_image("sha256:" + "b" * 64, "d" * 40, "c" * 64, mismatched)


if __name__ == "__main__":
    unittest.main()
