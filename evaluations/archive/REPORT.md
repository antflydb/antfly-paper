# OfficeQA Pro v2 archive results

Across a full encoder replacement, Antfly and LanceDB each served every scheduled A query at two arrivals per second. Antfly reached searchable B coverage in 97.7 minutes and LanceDB in 94.2 minutes. Neither returned an ineligible page. These are single native macOS trials on Wolfspider with a shared application-side embedding worker. The controlled native alias and durable-outbox experiments establish the separate database lifecycle claims. [Aggregate receipts](observations/officeqa-native-summary.json) · [Native contracts](../native-contracts/README.md)

Wolfspider has a 14-core Apple M4 Max and 36 GiB of unified memory. The native runs used macOS arm64, the official Antfly 0.3.1 binary (SHA-256 `c8f04db1564bf8a82d39999cb7f64798685bfbfa806827a7c41055c9bfe09e7d`), LanceDB 0.41.0 and PyArrow 26.0.1. Publisher text mapped to 115,458 of the 118,524 physical pages. Generation A used `BAAI/bge-small-en-v1.5` and B used `BAAI/bge-small-en`. The corpus preparation and timed worker used the same model weights.

The [comparability audit](COMPARABILITY.md) covers all five evolved A indexes, access boundaries, cutover semantics and latency accounting.

## Work completed during migration

| Native macOS trial | Antfly v0.3.1 | LanceDB Embedded |
| --- | ---: | ---: |
| Distinct chunks re-embedded with B | 251,707 | 251,707 |
| Physical rows searchable in B | 616,832 | 616,832 |
| B model batches | 7,866 | 7,866 |
| Wall time inside B embedding calls | 89.77 min | 91.97 min |
| Outside model calls through the last B write acknowledgement | 7.86 min | 2.09 min |
| Last write acknowledgement to verified B readiness | 1.10 s | 10.43 s |
| Last B write acknowledged | 97.63 min | 94.05 min |
| B row counts complete and first two-channel probe passed | 97.65 min | 94.23 min |
| Client switch plus first verified B read | 221 ms | 64 ms |
| Scheduled A arrivals during migration | 11,719 | 11,308 |
| Failed or missing arrivals | 0 | 0 |
| A p95 before migration (408 arrivals) | 466 ms | 815 ms |
| A p95 during migration | 455 ms | 825 ms |

Both engines consumed the same publisher-text corpus and the same frozen query vectors. The worker re-embedded each distinct chunk once with the replacement BGE encoder, checked its direction against the prepared vector and wrote public and tenant copies. Model time measures client wall time inside embedding calls, including the round trip. Antfly acknowledged B batches at the write barrier, then checked full-text and vector coverage. LanceDB finalized its indexes and checked the row count. Each adapter issued the same eligible lexical and dense probe before cutover. The duration of the build determined how many A arrivals were scheduled. [Runner and reproduction instructions](README.md#run-one-comparison)

Antfly reached readiness 205.5 s later than LanceDB. Its model calls took 131.6 s less, but it spent 346.4 s more outside those calls before the last write acknowledgement. It then reached verified readiness 9.3 s sooner. The before-acknowledgement interval includes preparing and serializing rows, making insert calls and waiting for acknowledgements. The historical receipts combine those costs. The loader used 128-row Antfly batches and 2,048-row LanceDB batches. A separate frozen-vector control on 616,832 rows recorded 402.78 s inside Antfly write calls versus 219.89 s for OpenSearch with document-level security, both with four CPUs and 4 GiB. Write-call time accounted for 182.89 s of that pair's 192.04 s difference to readiness. Only the first Antfly policy trial completed. These observations identify the write path for profiling. An attribution to batch size or a specific storage subsystem requires a matched sweep with per-batch timing. [Full-corpus control receipts](observations/officeqa-matched-followups.md) · [Loader and batch sizes](compare.py)

![Acknowledged B rows and A query latency during migration](observations/officeqa-native-migration.png)

Each point aggregates one minute of the build. The last bin ends at readiness and may include fewer arrivals. The bundled [plotting script](migration_figure.py) reads the aggregate JSON directly.

In a separate release-to-commit check with scoped API keys, all 408 evolved hybrid queries completed while 22,528 frozen B rows were indexed. Client p95 fell from 383 ms on v0.3.2 to 240 ms on ReleaseFast commit `7f6031aebd`. The ordered physical-page results matched for every query. [Paired protocol and results](COMPARABILITY.md#releasefast-backport-of-1756)

In a separate uninstrumented comparison, #1814 lowered pooled serving-only HTTP p95 from 120.84 to 93.94 ms on the same 616,832-row snapshot. During backfill to a second table, it fell from 101.74 to 83.76 ms. All 3,264 requests completed with identical results. All write batches completed, with more write retries on the merged build. The packaged v0.3.3 source includes #1814; the timing subjects were the merge and its immediate parent. [Protocol and per-run results](observations/officeqa-hybrid-1814.md) · [Aggregate receipt](observations/officeqa-hybrid-1814.json)

## Which evidence pages came back?

The corpus has 1,435 PDFs, 118,524 physical pages, 90 original questions and 823 distinct cited question–page pairs. Each schema phase has 408 eligible tenant/filter variants. The client requests 100 candidates per channel and returns up to 20 distinct physical pages. Recall is the mean fraction of each variant’s eligible cited pages retrieved. “All” is the fraction of variants for which every eligible cited page was returned. [Corpus verification](observations/corpus-verification.json)

| Evolved tenant schema | Antfly recall@20 | LanceDB recall@20 | Antfly all@20 | LanceDB all@20 |
| --- | ---: | ---: | ---: | ---: |
| A lexical | 19.72% | 18.12% | 12.75% | 11.27% |
| A dense | 15.64% | 16.03% | 8.82% | 8.82% |
| A hybrid | 22.76% | 21.88% | 13.48% | 13.73% |
| B lexical | 19.96% | 18.12% | 12.75% | 11.27% |
| B dense | 15.38% | 15.43% | 8.82% | 8.82% |
| B hybrid | 23.76% | 21.91% | 14.22% | 13.97% |

Hybrid retrieval produced the highest mean recall in each trial. Giving each original question equal weight, the paired Antfly-minus-LanceDB hybrid recall difference was +1.22 percentage points for A (95% bootstrap interval −0.97 to +3.82) and +1.62 for B (−0.64 to +4.07). Both intervals include zero. The intervals resample the 90 original questions and quantify question variation. Each timing comes from one machine run per engine. Antfly enforced the tenant restriction with authenticated database readers. LanceDB used the trusted application's prefilter. [All quality cells, paired intervals and one-minute migration series](observations/officeqa-native-summary.json)

## Original questions without the tenant overlay

For the 90 original questions, the reader can search all public pages. Generation-A hybrid recall@20 was 14.17% for Antfly and 14.06% for LanceDB. Each system returned every cited page for 4 of 90 questions. This measures retrieval of the citations supplied by the dataset. End-to-end answer accuracy requires running the reader and scoring its answer. [Original-question scores](observations/officeqa-original-recall.json)

| Channel | Antfly recall@20 | LanceDB recall@20 |
| --- | ---: | ---: |
| Lexical | 10.99% | 11.02% |
| Dense | 7.85% | 8.48% |
| Hybrid | 14.17% | 14.06% |

## Scope of the other evidence

| Evidence | Coverage and interpretation |
| --- | --- |
| Five-engine retrieval qualification | All five adapters recovered the planted evidence on the public fixture. These checks establish wiring and filtering on known inputs. [Receipt](observations/matched-fixture.json) |
| Controlled backfill | With 1,024 vectors, the medians of three trials were 25.79 s for Antfly and 12.69 s for the PostgreSQL outbox. The receipts include all trials and exclusions. [Backfill receipts](observations/backfill-v031.json) |
| Serving with tenant API keys | On the 1,024-record fixture, Antfly v0.3.3 database-request p95 was 23.7–27.1 ms serving alone and 39.3–89.1 ms over the backfill window. [Serving latency and per-arrival data](observations/document-reader-auth-v033.md) |
| Remote-object OCR and graph correctness | Both Antfly and PostgreSQL passed the authored scanned-ledger correction and intermediate-node revocation cases with the shared PDF and extraction frontend. [Antfly](observations/antfly-raw-final.json) · [PostgreSQL](observations/postgres-raw-final.json) |
| Full-corpus PostgreSQL, OpenSearch and Vespa | OpenSearch and Vespa completed the original Linux-VM cohort. PostgreSQL completed a separate follow-up using readiness probe 284. [Campaign report](observations/officeqa-wolfspider-campaign.md) · [PostgreSQL follow-up](observations/officeqa-matched-followups.md) |
| Original-question answers and real-page extraction | End-to-end exact answer scores require reader outputs. Graph precision and recall require independent adjudicated page judgments. The publisher-text retrieval table above uses citation judgments. |

In a separate v0.3.1 check, one standalone Antfly instance took three PDF URLs and two enrichment definitions. Native extraction handled both born-digital pages, LightOnOCR-3-0.8B handled the scanned page, and the native chunker produced four searchable chunks in total. The matched application worker reported 2,094 prompt tokens and 19 completion tokens for the scan. These token counts belong to its inference API request. [Configuration](lighton-enrichments.json) · [Fixture receipt](observations/lighton-ingestion-fixture.json)

### Page-image to text-chunk migration

#### Application code and service counts on the full PDF workload

The full representation experiment starts with the same 1,435 versioned PDFs and 118,524 physical pages in each arm. Multiple tenant views produce 3,579 source rows and a target of 290,936 A vectors. The table counts the pipeline used during the timed builds. The reference-vector preparation job and the traffic generator are measurement infrastructure.

| Responsibility | Native Antfly | Application-managed arms |
|---|---|---|
| Fetch, render, extract, chunk and embed PDFs | Antfly runs the producers described by two named indexes and two enrichment definitions. Application code submits versioned URLs, sets up the indexes and readers, checks coverage and requests the alias switch. | One shared Python producer runs inside the client for all five engines. Its outer module has 119 physical lines containing code. It fetches each PDF, renders and embeds pages for A, and extracts and embeds passages for B. Imported model, chunking and HTTP helpers and each engine's adapter are additional code. |
| Separately deployed endpoints in the timed replay | 3: Antfly standalone, the versioned object server and the replay reader. Antfly hosts embedding inference. | 4 for PostgreSQL, OpenSearch, Vespa and the application-managed Antfly arm: the database, object server, replay reader and embedding service. Embedded LanceDB uses the last three, with its database in the client process. |
| Routing after B reaches coverage | The client updates a database alias guarded by coverage. | The client changes the table and query representation it selects. |

The primary deployment comparison is native Antfly versus PostgreSQL/pgvector. Both require tenant setup, ingestion, a readiness decision, cutover and a query client. In PostgreSQL the application owns object-version checks, transformation calls, model-identity checks, tenant fan-out and the routing decision. Native Antfly takes versioned source references and declares both producers and the expected index for the alias update. Each implementation checks the returned physical page against the same tenant judgments. The operator provisions the database, object source and replay reader for both, plus a model endpoint for PostgreSQL. The benchmark records deployment roles and executed code paths. It does not measure human time.

Run `python3 evaluations/archive/pdf_code_inventory.py` from the repository root for token-bearing source lines, by deployment and responsibility. The inventory includes the 119-line application PDF producer, imported transformation helpers, database adapters, and the mixed client/instrumentation drivers on both sides. It lists shared services and measurement code separately. Whole-driver counts include branches used by other subjects. They cannot be summed into a measure of backend effort. The two native enrichment declarations are JSON, and both native index declarations are built by one Python function. [Source census](pdf_code_inventory.py) · [Paired protocol and write diagnostic](COMPARABILITY.md#full-pdf-deployment-comparison)

The original live producer module had SHA-256 `5ea8b5c7f73a5b76ebcf17fc69314cc7ca99e5a0e0dc7aa9e5016f6fba2676b2`. [Application producer](pdf_pipeline.py) · [Native index and alias operations](pdf_native_archive.py) · [Enrichment configuration](lighton-enrichments.json) · [Service startup](document_study.py) · [Engine deployments](pdf_archive_campaign.py)

The v0.3.3 replay uses the packaged Darwin arm64 binary from source commit `2d456b7cecc05906b96038fa52859f03472967ad`. A synthetic pagination check returned all 208 passages from one long page, each with a 768-dimensional EmbeddingGemma 2 vector. At 09:11 PST on November 25, generation A of the native full-corpus trial had 257,516 searchable vectors against a target of 290,936. It reported 3,512 covered tenant-visible source rows out of 3,579, with 67 pending and zero failed. The application-side preparation retained 1,240 PDF checkpoints after a broken pipe. Its retry was queued behind the native trial on the exclusive host. End-to-end results for this migration are pending. [Binary checksums and protocol](README.md#publisher-replay-and-measured-raw-pdf-sample)

The November 22 protocol uses publisher-text replay for the full 118,524-page representation migration. The versioned page catalog contains 115,458 mapped pages and 3,066 explicit missing-text entries. Generation A retains every physical page and the original citation judgments include missing-text pages. The full trial times retrieval of remote PDFs, indexing, serving during replacement, and cutover using native PDF text where available and publisher replay on OCR fallback for generation B. Its timing includes fallback page rendering, upload and replay lookup. In the v0.3.2 qualification a three-page PDF was served to two tenant-visible source rows. Antfly issued two mapped and two missing-page replay requests, recorded zero OCR failures, and returned physical page 1 for a search term supplied only by the replay provider. [Qualification receipt](observations/publisher-replay-v032.json) [Reproduction](README.md#publisher-replay-and-measured-raw-pdf-sample)

All five application adapters passed a separate three-PDF qualification. Each fetched the PDFs inside both timed builds, read embedded text from two pages, uploaded the scanned third page to the replay reader, switched to eight searchable passage rows, and retrieved the cited page by a replay-only term. [Five-adapter receipt](observations/publisher-replay-adapters-v032.json)

A separate real-OCR sample freezes one whole PDF from each of five length strata. The selected PDFs have 1, 13, 21, 45 and 684 pages, totaling 764. Publisher markup identifies numeric tables on 732 of those pages. The native LightOnOCR trial published 2,225 visual vectors. During passage backfill one of 12 tenant-visible sources failed, two were covered, and nine were still pending when the trial ended. The shared worker checkpointed three PDFs (35 physical pages) before the study switched to full publisher replay. Remaining real-OCR trials were skipped. The 90 original OfficeQA questions are evaluated against the full archive.

The exhaustive application OCR worker was checkpointed after 667 PDFs and 3,406 pages. In an earlier six-hour window it committed 626 pages (104.3 pages/hour). At that constant rate the complete shared preparation would take about 47 days of continuous service on Wolfspider. Document ordering favors short PDFs and the rate includes embedding. This is a planning estimate. The partial sample does not support a full-corpus OCR cost estimate.

Antfly v0.3.2 passes the native PDF migration fixture. The single-PDF reproducer for #1758 returned the physical page in both source-grouped and direct queries. On three PDF URLs, all three visual pages were retrievable and all 112 tenant-reader probes returned results during migration to four passage vectors. The correction, revocation, hybrid retrieval and two-page citation checks passed. The application-rendered control passed all eight checks. The v0.3.2 Antfly adapter also passed all seven shared-input checks using the exact inputs previously evaluated on PostgreSQL, OpenSearch, Vespa and LanceDB. These are local fixture runs. The November 20 run used real OCR on the frozen archive inventory. The checkpoint and sample results above describe how far it progressed before full-corpus evaluation switched to publisher-text replay. [v0.3.2 receipts and binary digest](observations/pdf-vector-migration-v032.json)

On November 21, a text-layer census read every physical page with the same PDFium extraction call as the shared-input worker. All 118,524 pages yielded zero non-whitespace characters, so every page qualifies for LightOnOCR under the worker’s 50-character threshold. The native Antfly trial records its own OCR decisions.

On November 21 the shared worker switched to an 8,192-token first call after 361 completed PDFs (898 pages). The earlier policy issued 1,219 OCR calls, including 321 truncated attempts that triggered retries. Those documents took 29,914 seconds of active preparation time (108.1 pages/hour). Use `--legacy-ocr-pdfs 361` to replay the same boundary. Report throughput for subsequent PDFs separately. The input order favors short documents, so a before-and-after rate alone cannot establish a speedup.


On the public fixture, v0.3.1's native PDF page-image index reported complete coverage of three sources and three searchable vectors. Its dense profile recorded three raw matches, but the query returned zero hits to the administrator and the tenant reader. The same three pages rendered to PNG in the application and embedded by Antfly returned three hits with the same EmbeddingGemma 2 query vector. Release v0.3.1 therefore fails the entry condition for the full native OfficeQA representation comparison. [Fixture receipt](observations/pdf-vector-migration-v031.json)

With the PNG starting index, all 36 live reader probes returned results during replacement. The native replacement fetched the PDFs, selected LightOnOCR-3-0.8B for the scanned ledger, and indexed four text chunks under the same model identity. During backfill the client corrected that ledger and moved the appendix out of the reader's tenant. After cutover the reader found the correction and could retrieve only the two eligible sources. In a separate two-page probe, the replacement ranked physical page 2 first for evidence unique to that page. These fixture checks establish behavior on authored inputs. Full-corpus recall, compute cost, and cross-engine latency for this migration remain unmeasured. [Reproduction](README.md#pdf-page-image-to-passage-index-qualification)

All five engines passed the shared-input representation fixture. Antfly, PostgreSQL/pgvector, OpenSearch, Vespa and LanceDB each returned the planted page first from the three image vectors in A and the four text-chunk vectors in B. Each retrieved the corrected scan, hid the revoked appendix from the public reader, and ranked physical page 2 first for the additional multi-page probe. The application supplied identical vectors and switched the serving generation on all five engines. Antfly and PostgreSQL enforced tenant access in the database. The other adapters applied the tenant predicate in the application query. Reader probes ranged from 1 to 55 per engine. The shared worker made two OCR calls across the two scan revisions, consuming 4,188 prompt and 41 completion tokens in total. [Five-engine receipts](observations/pdf-vector-comparison.json) · [Native PDF retrieval issue](https://github.com/antflydb/antfly/issues/1758)
