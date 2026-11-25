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
    def test_model_aware_passages_follow_cursor_and_reject_incomplete_coverage(self):
        from pdf_vector_compare import passage_vectors

        def page(start, complete):
            return {
                "plan_id": "plan",
                "model_identity": "digest",
                "gaps": [],
                "task_type": "RETRIEVAL_DOCUMENT",
                "dimensions": 768,
                "source_complete": complete,
                "status": "complete" if complete else "partial",
                "start_ordinal": start,
                "covered_through_ordinal": start + 1,
                "next_continuation": None if complete else "cursor",
                "data": [
                    {
                        "descriptor": {"ordinal": start, "input_tokens": 150},
                        "text": str(start),
                        "embedding": [0.5] * 768,
                    }
                ],
            }

        def respond(_url, _method, req):
            self.assertEqual(req["policy"], {"target_tokens": 160, "overlap_tokens": 0})
            self.assertEqual(req["embedding"]["model_identity"], "digest")
            return (
                page(1, True) if req.get("continuation") == "cursor" else page(0, False)
            )

        with patch("pdf_vector_compare.call", side_effect=respond):
            self.assertEqual(
                [t for t, _ in passage_vectors("http://local", "ab", "digest")],
                ["0", "1"],
            )
        for invalid in (
            {"gaps": [1]},
            {"model_identity": "other"},
            {"covered_through_ordinal": 0},
            {"status": "partial", "source_complete": False, "next_continuation": None},
        ):
            with (
                self.subTest(invalid=invalid),
                patch("pdf_vector_compare.call", return_value=page(0, True) | invalid),
            ):
                with self.assertRaises(ValueError):
                    list(passage_vectors("http://local", "ab", "digest"))

    def test_native_hybrid_uses_one_scoped_request_and_checks_pages(self):
        from engines import Antfly
        from latency_backfill import measure

        q = {"tenant": "public", "question": "needle", "a": [0.5]}
        with patch.dict("os.environ", ARCHIVE_ANTFLY_AUTH="admin:test"):
            engine = Antfly("http://local", "snapshot", access="api_key")
        engine.readers["public"] = "reader"
        hit = {"_id": "id", "_score": 1.5, "_source": {"page": "doc#page=1"}}
        result = {"responses": [{"status": 200, "hits": {"hits": [hit]}}]}
        with patch("engines.call", return_value=result) as request:
            row = measure(engine, q, {"doc"}, True, 0)
            request.assert_called_once()
            endpoint, method, body, credential = request.call_args.args
            self.assertEqual(
                (endpoint, method, credential),
                ("http://local/db/v1/query", "POST", "reader"),
            )
            self.assertEqual(
                body["full_text_search"], {"match": "needle", "field": "body"}
            )
            self.assertEqual(body["embeddings"], {"vec": [0.5]})
            self.assertEqual(row["hits"], 1)
            self.assertEqual(
                len({row[k] for k in ("digest", "ids_digest", "score_digest")}), 3
            )
            with self.assertRaisesRegex(ValueError, "ineligible candidate"):
                measure(engine, q, set(), True, 0)

    def test_paced_backfill_retries_committed_identity(self):
        from latency_backfill import backfill

        engine = Mock()
        throttle = RuntimeError('{"retryable":true,"retry_after_ms":1250}')
        throttle.__cause__ = HTTPError("url", 429, "backpressured", {}, None)
        engine.insert.side_effect = [throttle, None]
        batch = [{"id": "one"}]
        with (
            patch("latency_backfill.time.monotonic", return_value=1),
            patch("latency_backfill.time.sleep") as sleeper,
        ):
            writes, retries, error = backfill(engine, [batch], 1, 1.2)
        self.assertEqual((writes, retries, error), ([[0, 0]], 1, None))
        self.assertEqual(
            [args.args[0] for args in engine.insert.call_args_list], [batch, batch]
        )
        sleeper.assert_any_call(1.25)

    def test_backfill_arrival_cap_and_completed_percentiles(self):
        from latency_backfill import summarize, traffic

        pool = Mock()
        pending = Mock()
        pending.done.return_value = False
        pool.submit.side_effect = lambda fn, q, due: Mock(
            done=pending.done,
            result=lambda: {
                "id": q["id"],
                "scheduled": due,
                "digest": "rank",
                "latency": 0.03,
                "channels": {"lexical": 0.01, "dense": 0.02},
            },
        )
        with (
            patch("latency_backfill.ThreadPoolExecutor") as executor,
            patch("latency_backfill.pause_until"),
        ):
            executor.return_value.__enter__.return_value = pool
            rows = traffic(None, [{"id": str(i)} for i in range(10)], {}, 0)
        data = summarize(rows)
        self.assertEqual(
            (data["scheduled"], data["completed"], data["missed"]), (10, 8, 2)
        )
        self.assertEqual(data["p95_ms"], 30)
        self.assertEqual(
            {row["id"] for row in rows if row.get("error") == "concurrency_limit"},
            {"8", "9"},
        )
        self.assertIsNone(summarize([{"error": "ineligible"}])["p95_ms"])

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
