import tempfile
import unittest
from pathlib import Path

from annotate import assertions, sample
from corpus import chunk_rows, publisher_pages
from evidence import citations, fuse, score_assertions, score_pages
from miniature import build


class ArchiveTests(unittest.TestCase):
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
