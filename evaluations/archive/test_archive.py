import hashlib
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from annotate import assertions, sample
from compare import observations
from corpus import chunk_rows, publisher_pages
from evidence import citations, fuse, score_assertions, score_pages
from miniature import build
from pdf_archive_inputs import bind_ocr_schedule


class ArchiveTests(unittest.TestCase):
    def test_ocr_schedule_resume(self):
        inventory = [{"path": name} for name in ("a.pdf", "b.pdf")]
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            spec = {"subject": "release"}
            marker = root / "identity.json"
            marker.write_text(json.dumps(spec | {"ocr_retry_truncated_tokens": 8192}))
            receipts = root / "receipts"
            receipts.mkdir()

            def receipt(name):
                return receipts / (hashlib.sha256(name.encode()).hexdigest() + ".json")

            receipt("b.pdf").touch()
            with self.assertRaisesRegex(ValueError, "prefix"):
                bind_ocr_schedule(root, spec, inventory, None)
            receipt("b.pdf").rename(receipt("a.pdf"))
            frozen = bind_ocr_schedule(root, spec, inventory, None)
            self.assertEqual(frozen["legacy_ocr_pdfs"], 1)
            receipt("b.pdf").touch()
            self.assertEqual(bind_ocr_schedule(root, spec, inventory, None), frozen)
            with self.assertRaisesRegex(ValueError, "identity"):
                bind_ocr_schedule(root, spec, inventory, 2)
            with self.assertRaisesRegex(ValueError, "identity"):
                bind_ocr_schedule(root, {"subject": "other-release"}, inventory, None)

    def test_direct_ocr_ceiling(self):
        from common import extract_page

        page = Mock()
        page.get_textpage.return_value.get_text_range.return_value = ""
        image = page.render.return_value.to_pil.return_value
        image.width = image.height = 1
        truncated = OSError('"OUTPUT_TRUNCATED"')
        truncated.__cause__ = HTTPError("url", 400, "Bad Request", {}, None)
        costs = Counter()
        with patch("common.request", side_effect=truncated) as request:
            result = extract_page(
                page, 1, "url", reader="reader", costs=costs, ocr_token_limits=(8192,)
            )
            self.assertEqual(result["extraction"], "ocr_truncated")
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.args[1]["max_tokens"], 8192)
            self.assertEqual((costs["ocr_calls"], costs["ocr_truncated_calls"]), (1, 1))

    def test_citations(self):
        for source in (
            "corpus_file=a.txt | pdf_page_number=2 | description=see lamps; or rope",
            "N/A 1971 2 a.txt, Table A",
            "a page 2",
        ):
            self.assertEqual(
                citations({"uid": "q", "source_docs": source}, {"a.pdf": 3}),
                {("a.pdf", 2)},
            )
        with self.assertRaises(ValueError):
            citations({"uid": "q", "source_docs": "a page 0"}, {"a.pdf": 3})

    def test_rankings_and_oracles(self):
        key = "a.pdf#page=1"
        gold = {"q": {"eligible": ["a.pdf"], "pages": [key, "a.pdf#page=2"]}}
        metric = score_pages(gold, {"q": [key, key, "a.pdf#page=2", "b.pdf#page=1"]}, 2)
        self.assertEqual((metric["complete"], metric["ineligible"]), (1, 1))
        self.assertEqual(fuse([["b", "a"], ["a", "b"]], 2), ["a", "b"])
        with self.assertRaises(ValueError):
            score_pages(gold, {}, 1)
        support = [["harbor", "paid-for", "lamps", key]]
        self.assertEqual(score_assertions(support, [])["missing"], [tuple(support[0])])
        with self.assertRaises(ValueError):
            assertions({}, [key])

    def test_below_cutoff_candidate_leak(self):
        engine = Mock()
        engine.search.return_value = [
            ("ok", "a.pdf#page=1"),
            ("hidden", "b.pdf#page=1"),
        ]
        with self.assertRaisesRegex(ValueError, "ineligible candidate"):
            observations(
                engine, {"id": "q", "a": [1, 0]}, "a", "lexical", 10, 1, {"a.pdf"}
            )

    def test_physical_pages_and_stream(self):
        payload = {
            "document": {
                "elements": [
                    {"content": "ledger", "bbox": [{"page_id": 0}, {"page_id": 0}]},
                    {"content": "appendix", "bbox": [{"page_id": 2}]},
                ]
            }
        }
        self.assertEqual(
            [(p["page"], p["body"]) for p in publisher_pages(payload)],
            [(1, "ledger"), (3, "appendix")],
        )
        inventory = {"files": [{"path": "pdfs/a.pdf", "physical_pages": 3}]}
        self.assertEqual(len(set(sample(inventory, 2, "trial"))), 2)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "fixture"
            build(root)
            self.assertEqual(len(list(chunk_rows(root))), 3)


if __name__ == "__main__":
    unittest.main()
