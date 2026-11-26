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
    def test_helper_stops_before_primary_and_resumes_partial_import(self):
        from pdf_archive_inputs import (
            assist_has_room,
            document_key,
            import_assisted_pdf,
        )

        with tempfile.TemporaryDirectory() as directory:
            primary = Path(directory) / "primary"
            receipts = primary / "receipts"
            receipts.mkdir(parents=True)
            pdfs = [{"path": f"fixture-{i}.pdf"} for i in range(5)]
            self.assertTrue(assist_has_room(primary, pdfs, 4))
            (receipts / (document_key(pdfs[1]) + ".json")).touch()
            self.assertTrue(assist_has_room(primary, pdfs, 4))
            (receipts / (document_key(pdfs[0]) + ".json")).touch()
            self.assertFalse(assist_has_room(primary, pdfs, 4))

            staged = Path(directory) / "staged"
            staged.mkdir()
            files = {}
            hashes = {}
            for gen in ("a", "b"):
                dest = primary / gen / "documents"
                dest.mkdir(parents=True)
                path = staged / f"{gen}.jsonl"
                path.write_bytes(gen.encode())
                files[gen] = path
                hashes[gen] = hashlib.sha256(gen.encode()).hexdigest()
            receipt = staged / "fixture.json"
            receipt.write_text(json.dumps({"sha256": hashes}))
            # Recover a prior interruption after moving only the first shard.
            files["a"].rename(primary / "a/documents/a.jsonl")
            files["b"].write_bytes(b"damaged")
            with self.assertRaisesRegex(ValueError, "checksum"):
                import_assisted_pdf(primary, files, receipt)
            self.assertFalse((receipts / receipt.name).exists())
            files["b"].write_bytes(b"b")
            import_assisted_pdf(primary, files, receipt)
            self.assertEqual(
                json.loads((receipts / receipt.name).read_text())["sha256"], hashes
            )
            import_assisted_pdf(primary, files, receipt)

    def test_two_helpers_hold_distinct_leases_and_reclaim_after_exit(self):
        from contextlib import closing

        from pdf_archive_inputs import assist_order

        with tempfile.TemporaryDirectory() as directory:
            primary = Path(directory)
            (primary / "receipts").mkdir()
            pdfs = [{"path": str(i)} for i in range(6)]
            with closing(assist_order(primary, pdfs)) as first:
                self.assertEqual(next(first), 5)
                with closing(assist_order(primary, pdfs)) as second:
                    self.assertEqual(next(second), 4)
            with closing(assist_order(primary, pdfs)) as resumed:
                self.assertEqual(next(resumed), 5)

    def test_pdf_protocol_versions_and_frozen_corpus(self):
        from evidence import is_pdf_migration

        self.assertFalse(is_pdf_migration({"source": "publisher"}))
        for version in (1, 2):
            self.assertTrue(
                is_pdf_migration(
                    {"representations": f"pdf-images-to-passages-v{version}"}
                )
            )
        with self.assertRaises(ValueError):
            is_pdf_migration({"representations": "unrecognized"})

    def test_scheduled_reads_and_public_summary(self):
        import threading
        import time
        from types import SimpleNamespace

        from compare import scheduled_reads
        from document_study_report import traffic_summary

        page = "private.pdf#page=1"
        probe = {"id": "private-question", "a": [1]}
        rows = scheduled_reads(
            Mock(search=Mock(return_value=[("key", page)])),
            [probe],
            {probe["id"]: {"eligible": ["private.pdf"], "pages": [page]}},
            SimpleNamespace(qps=100, depth=5, k=1),
            "dense",
            time.monotonic(),
            threading.Event(),
            3,
            compact=True,
        )
        self.assertEqual([r["arrival"] for r in rows], [0, 0.01, 0.02])
        self.assertTrue(
            all(r["cited_page_returned"] and r["hit_count"] == 1 for r in rows)
        )
        summary = traffic_summary(
            rows + [{"error": "load generator at concurrency limit"}], 5
        )
        self.assertEqual(
            (
                summary["completed"],
                summary["returned_citation"],
                summary["capacity_drops"],
                summary["missed"],
            ),
            (3, 3, 1, 1),
        )
        self.assertNotIn("private", json.dumps(summary))

    def test_sweep_balance_and_final_partial_batch(self):
        import io

        from compare import load
        from write_batch_sweep import plan

        order = plan()
        self.assertEqual(
            [p[1:] for p in order[:6]], [p[1:] for p in reversed(order[6:])]
        )
        self.assertEqual(len(set(p[1:] for p in order)), 6)
        rows = [
            {"id": str(i), "page": "p.pdf#page=1", "views": {}, "body": "x", "b": [1]}
            for i in range(129)
        ]
        log = io.StringIO()
        self.assertEqual(
            load(
                Mock(), None, "b", True, live_rows=rows, batch_size=128, batch_log=log
            ),
            129,
        )
        entries = [json.loads(s) for s in log.getvalue().splitlines()]
        self.assertEqual(
            [(e["first_row"], e["rows"]) for e in entries], [(0, 128), (128, 1)]
        )

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

    def test_offline_catalog_matches_http_fallback(self):
        from pdf_archive_inputs import catalog_extract
        from pdf_pipeline import extract
        from publisher_replay import page_text

        page = Mock()
        catalog = sqlite3.connect(":memory:")
        self.addCleanup(catalog.close)
        catalog.execute("CREATE TABLE pages(sha,page,body, PRIMARY KEY(sha,page))")
        catalog.executemany(
            "INSERT INTO pages VALUES ('v',?,?)",
            [(1, "  value\n"), (2, None), (3, "  ")],
        )
        for number, layer, mode in [
            (1, "x " * 49, "publisher"),
            (2, "", "publisher_missing"),
            (3, "", "publisher"),
            (4, " z" * 50, "native"),
        ]:
            with self.subTest(number=number):
                page.get_textpage.return_value.get_text_range.return_value = layer
                page.render.reset_mock()
                counts = Counter()
                direct = catalog_extract(page, number, catalog, "v", counts)
                self.assertEqual(direct["extraction"], mode)
                self.assertEqual(
                    counts["publisher_catalog_lookups"], int(mode != "native")
                )
                page.render.assert_not_called()
                image = page.render.return_value.to_pil.return_value
                image.width, image.height = 8, 8
                image.save.side_effect = lambda dest, fmt: dest.write(b"raster")
                with patch(
                    "common.request",
                    side_effect=lambda *a, number=number, **kw: {
                        "data": [{"text": page_text(catalog, "v", number) or ""}]
                    },
                ) as request:
                    self.assertEqual(
                        direct,
                        extract(
                            page,
                            number,
                            None,
                            (),
                            catalog,
                            "v",
                            Counter(),
                            "http://reader",
                        ),
                    )
                    self.assertEqual(request.call_count, int(mode != "native"))
        page.get_textpage.return_value.get_text_range.return_value = ""
        with self.assertRaisesRegex(ValueError, "unknown source"):
            catalog_extract(page, 1, catalog, "wrong", Counter())

    def test_replay_transport_upgrade_preserves_source_identity(self):
        from pdf_archive_inputs import bind_replay

        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            spec = {"reader": "publisher", "acquisition_sha256": "a"}
            marker = out / "identity.json"
            marker.write_text(
                json.dumps(spec | {"replay_transport": "source-page-png-v1"})
            )
            bind_replay(out, spec)
            self.assertEqual(json.loads(marker.read_text()), spec)
            for changed in (
                spec | {"acquisition_sha256": "b"},
                spec | {"replay_transport": "unknown"},
            ):
                marker.write_text(json.dumps(changed))
                with self.assertRaisesRegex(ValueError, "identity changed"):
                    bind_replay(out, spec)

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
