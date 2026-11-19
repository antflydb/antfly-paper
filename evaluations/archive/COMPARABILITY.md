# What the OfficeQA experiments compare

The five adapters answer the same evidence questions. They implement different search and access paths. Treat retrieval quality as a comparison of the specified configurations. Treat elapsed times as observations of the deployed systems. An engine speed ranking requires matched resource limits, access enforcement, retrieval quality, cache conditions and repeated runs.

## Retrieval on the evolved schema

All five A indexes used the same publisher text, chunk identities, query vectors, tenant/filter variants, eligible citations, top-100 candidate budget per channel, reciprocal-rank fusion and 20-page return budget. All 408 query variants contribute to the results, including empty channels. The independent judgments contained 90 underlying questions. [Scorer and resampling procedure](compare_receipts.py) · [Aggregate results](observations/officeqa-comparability.json)

| Configuration | Hybrid mean page recall@20 | Every cited page@20 | Empty lexical / dense channels | Tenant boundary |
| --- | ---: | ---: | ---: | --- |
| Antfly v0.3.1 | 22.76% | 13.48% | 0 / 0 | Database row policy |
| LanceDB Embedded | 21.88% | 13.73% | 0 / 0 | Trusted application prefilter |
| PostgreSQL + pgvector | 11.98% | 6.37% | 396 / 0 | Database RLS |
| OpenSearch | 20.37% | 14.71% | 0 / 0 | Trusted application filter |
| Vespa | 15.43% | 9.31% | 0 / 20 | Trusted application filter |

OpenSearch retrieved all cited pages for more variants than Antfly, while Antfly had higher mean recall. Across the 90 equally weighted original questions, Antfly minus OpenSearch hybrid recall was +3.85 percentage points (paired bootstrap 95% interval +0.57 to +7.83). PostgreSQL's adapter passes the whole question to English `websearch_to_tsquery`, which ordinarily requires all the unquoted terms. OpenSearch uses a `match` query. Vespa uses `userInput` and a separate nearest-neighbor rank profile. Candidate depth counts returned candidates. Token matching, ranking, ANN search effort and prefilter implementations are engine-specific. A comparison of the fastest configurations at fixed recall would need a tuning set separate from these 90 questions. [Adapters](engines.py)

The original readiness gate required a nonempty result from each channel on the first evolved query. On that query PostgreSQL A returned zero lexical and 20 dense pages. Failure on B left PostgreSQL without a verified migration time. Twelve of the 408 queries returned hits on both channels on A in every system. The first such query was ordinal 284, selected retrospectively from A only. A separate run can use `--readiness-probe-ordinal 284` and retain all 408 queries for scoring and traffic. The runner checks both A rankings before building B. A future pre-registered cohort can instead plant a shared known-positive sentinel before any measurements.

## What the full-run clocks measure

| During migration | Antfly | OpenSearch |
| --- | ---: | ---: |
| B wall time | 97.65 min | 93.02 min |
| B model-call time | 89.77 min | 88.54 min |
| Remaining B wall time | 7.88 min | 4.49 min |
| Last write acknowledgment to verified readiness | 1.10 s | 0.28 s |
| A scheduled-arrival-to-completion p95 | 455 ms | 160 ms |
| A dispatch-to-completion p95 | 374 ms | 15 ms |
| Lexical / dense request p95 | 311 / 67 ms | 9 / 7 ms |
| Load generator dispatch delay p95 | 146 ms | 147 ms |

Both engines re-embedded 251,707 chunks in 7,866 shared-model calls and wrote 616,832 B rows. Antfly used the write barrier and checked full-text and vector counts. OpenSearch acknowledged bulk writes, refreshed, and checked the searchable document count. Both passed a two-channel probe. The older receipts combine decoding, vector verification, serialization, transport, insert calls and other work outside model calls. New receipts separately count and time the insert calls. A pure database write measurement can load the frozen B vectors; an end-to-end measurement includes the worker. Record both intervals for the same workload. [Migration runner](compare.py)

The client issues lexical and dense calls sequentially. During the build the same 408 A queries repeat at two arrivals per second. OpenSearch lexical p95 was already 34 ms before B started, versus 297 ms for Antfly. A fixed repeated-query workload, different cache state, Basic authentication per request, server-enforced policies on Antfly, and OpenSearch's application-enforced predicate all affect interpretation. Antfly ran natively on macOS and OpenSearch inside a 6-vCPU, 12-GiB Linux VM. These single trials identify the request path where time was spent. They cannot apportion the gap between those factors. Percentiles in different rows describe different distributions and do not add. [Full-run receipts](observations/officeqa-comparability.json)

## What the lifecycle and document evidence establishes

- The authored graph and revocation fixtures share the source bytes, OCR/extraction frontend, graph facts, expected paths and authorized readers. They establish correctness for the specified corrections and revocations. Accuracy on real pages awaits independent adjudication.
- The small native-alias and durable-outbox trials check held requests, fenced publication, visibility and independent vector probes. The full archive bulk trial verifies a client-selected table and matching encoder. An archive timing is evidence for bulk replacement, while the alias fixture supplies the pinning evidence.
- Reader exact-match scores require the original 90 questions, identical model revision and budgets, raw extraction and the same upstream scorer. The oracle uses the same reader with cited pages. Publisher-text recall and fixture OCR success cannot supply those scores.
- The original BGE-to-BGE migration uses two different 384-dimensional spaces. It can expose a wrong encoder even when its output fits the index. The BGE-to-EmbeddingGemma 2 arm uses 384 and 768 dimensions, fixes the text and chunking, and explicitly sends document and query task types. Swapping encoders changes both representation and storage footprint. The two arms answer different migration questions. [Release controls](../native-contracts/README.md)

The released native runtime re-embedded all three authored fixture chunks with Gemma using its document task. Both B indexes reported all eight physical rows, and the controller queried B using its query-task vector. This run validates the cross-dimensional data path; OfficeQA Gemma quality and migration time require a full-corpus run. [Gemma fixture receipt](observations/embeddinggemma2-fixture.json)

## Credential and access controls on one Linux VM

The public control generates 4,096 rows with 384-dimensional vectors. Each query has 32 eligible row IDs and 100 candidate slots. All four configurations returned all 32 on every measured lexical and dense call, with zero extra IDs. Four fresh trials rotate the configuration order. Each server has four CPU cores of quota and 4 GiB memory. A loopback client shares the server network namespace. One warm-up cycle is excluded. Within the protected Antfly run, the client alternates a user's Basic credential and an API key for that same user. Write calls alternate the admin credential types across 128-row batches. The policy arms omit the tenant predicate from the search request. OpenSearch enforces it using DLS on an isolated HTTP endpoint. [Runner](fairness_control.py) · [All trial receipts](observations/fairness-control.json) · [OpenSearch DLS](https://docs.opensearch.org/latest/security/access-control/document-level-security/)

| Configuration | Lexical p95 (ms) | Dense p95 (ms) | 128-row write p50 (ms) |
| --- | ---: | ---: | ---: |
| Antfly with row policy, Basic | 49.79 | 50.64 | 81.03 |
| Antfly with the same policy, API key | 1.28 | 2.14 | 33.93 |
| Antfly with application filter | 1.16 | 1.85 | 33.52 |
| OpenSearch with DLS, Basic | 3.97 | 9.91 | 60.80 |
| OpenSearch with application filter | 3.22 | 8.48 | 62.23 |

Cells are medians of four trial statistics. Basic credentials added about 49 ms per Antfly search and 47 ms per write batch compared with API keys on this fixture. This isolates the credential choice on the same protected table. Extrapolating those medians to a full corpus would confound credential cost with posting-list sizes, indexing, caching and concurrency. On one selected query in the retained full-size A index, six alternating replay rounds (first two excluded) gave 357 ms median client time and 303 ms median engine time for the sorted lexical plan. The dense path measured 68 and 9 ms respectively. A second independent four-round fixture campaign reproduced the credential effect. Basic versus API-key lexical p95 was 49.91 versus 1.30 ms, dense p95 was 50.73 versus 2.15 ms, and median batch write time was 81.63 versus 33.35 ms. All 16 trials again recovered the exact eligible sets. [Replication receipts](observations/fairness-control-repeat.json)

The full-size lexical trace includes time spent inside the engine. The matched Linux trials measure how much of that cost persists under comparable deployment and access settings. [Full-size trace](observations/officeqa-retention-profile.json)

The [matched OfficeQA follow-up report](observations/officeqa-matched-followups.md) covers two reverse-order Linux trials per access configuration using frozen B vectors. A separate PostgreSQL trial repeats fresh B inference and compares the original lexical parser with a disjunctive search after timing. Both retain all 408 questions and filter variants for scoring.
