import hashlib
import json
import sqlite3
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
    def test_replay_missing_and_conflicting_images(self):
        from publisher_replay import image_text, page_text

        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.executescript(
            "CREATE TABLE pages(sha,page,body); CREATE TABLE renders(sha,page,png);"
        )
        image = b"same rendered image"
        digest = hashlib.sha256(image).hexdigest()
        for sha in ["v1", "v2"]:
            db.execute("INSERT INTO renders VALUES (?,1,?)", (sha, digest))
            db.execute("INSERT INTO pages VALUES (?,1,NULL)", (sha,))
        self.assertIsNone(page_text(db, "v1", 1))
        self.assertIsNone(image_text(db, image))
        with self.assertRaisesRegex(ValueError, "unknown source"):
            page_text(db, "v3", 1)
        with self.assertRaisesRegex(ValueError, "unrecognized"):
            image_text(db, b"another page")
        db.execute("UPDATE pages SET body='text' WHERE sha='v2'")
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            image_text(db, image)

    def test_application_replay_only_uploads_fallback_pages(self):
        from pdf_pipeline import extract
        from publisher_replay import MODEL

        page = Mock()
        text = page.get_textpage.return_value.get_text_range
        text.return_value = "born digital " * 8
        costs = Counter()
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.executescript(
            "CREATE TABLE pages(sha,page,body); INSERT INTO pages VALUES ('version',3,NULL);"
        )
        with patch("common.request", return_value={"data": [{"text": ""}]}) as request:
            result = extract(page, 2, None, (), db, "version", costs, "http://reader")
            self.assertEqual(result["extraction"], "native")
            page.render.assert_not_called()
            request.assert_not_called()
            text.return_value = ""
            image = page.render.return_value.to_pil.return_value
            image.width, image.height = 4, 5
            image.save.side_effect = lambda stream, fmt: stream.write(b"raster")
            result = extract(page, 3, None, (), db, "version", costs, "http://reader")
            self.assertEqual(result["extraction"], "publisher_missing")
            url, payload = request.call_args.args
            self.assertEqual(url, "http://reader/pages/version/3/ai/v1/read")
            self.assertEqual(
                payload,
                {"model": MODEL, "images": [{"url": "data:image/png;base64,cmFzdGVy"}]},
            )
            self.assertEqual(
                (
                    costs["replay_calls"],
                    costs["replay_submitted_pixels"],
                    costs["ocr_calls"],
                ),
                (1, 20, 0),
            )

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
