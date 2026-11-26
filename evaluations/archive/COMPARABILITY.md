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

The original readiness gate required a nonempty result from each channel on the first evolved query. On that query PostgreSQL A returned zero lexical and 20 dense pages. Failure on B left PostgreSQL without a verified migration time. Twelve of the 408 queries returned hits on both channels on A in every system. The first such query was ordinal 284, selected retrospectively from A only. The completed PostgreSQL follow-up used `--readiness-probe-ordinal 284` and retained all 408 queries for scoring and traffic. The runner checks both A rankings before building B. A future pre-registered cohort can instead plant a shared known-positive sentinel before any measurements.

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

The [matched follow-up report](observations/officeqa-matched-followups.md) records five of ten planned frozen-vector Linux control runs. OpenSearch completed both rounds under each access configuration. Antfly completed one database-policy run with API keys. Five Antfly runs ended without a complete receipt. The separate PostgreSQL follow-up included fresh B inference and compared the original lexical parser with a disjunctive search after timing. All completed trials kept the 408 query variants for scoring.

### Why one filtered lexical query took 303 ms

On the restored 616,832-row A index, question 86 contains 100 whitespace-delimited words. With an API key, the original row policy, six-year retention and 100 requested hits, the native lexical executor took 303.72 ms. The entire engine trace took 304.51 ms. Medians cover seven randomly ordered rounds following two warmups. [Aggregate receipts](observations/officeqa-lexical-diagnosis.json)

| Change to that request | Median native execution |
| --- | ---: |
| Return the first hit only | 299.07 ms |
| Use the first 16 words, return 100 hits | 50.76 ms |
| Use the first / second 50 words, return 100 hits | 171.78 / 176.46 ms |
| Remove duplicate whitespace-delimited words, return 100 hits | 306.57 ms |

The 69-word deduplicated version returned the same 100 IDs in the same order. Shortening to either half changed the ranking. The paper workload uses the complete question.

A second experiment held the full question and top-one result fixed. Each request intersected the existing tenant and retention restrictions with either the best one, 20, or 100 IDs from the original answer. The trace confirmed the input cardinality at the ranker. Median execution times over nine measured rounds were **295 / 298 / 299 ms**, respectively. With all 59,273 eligible IDs the median was **301 ms**. A constant-score match-all control over those same 59,273 IDs took **19 ms**. One exploratory repetition overlapped heavy compilation on the host and had unstable wall times. The nine-round repeat independently recorded server CPU, which was 318 ms with one eligible ID and 317 ms with all 59,273.

A separate run of the linked replay script combined all 11 arms and verified every expected rank prefix. The full top-100 request took 323 ms in execution, the one-eligible-row request took 321 ms, and the first-16-word request took 53 ms.

The release passes the native positive filter to search as a sorted list of document numbers. The scorer's candidate-selection step uses bitmap restrictions to advance. Its later collector checks the list when admitting an already scored candidate. This identifies an opportunity to skip scoring excluded rows earlier. The share of the 303 ms that an implementation can recover requires a patched replay with identical scores, ranks and tenant membership. [Storage handoff](https://github.com/antflydb/antfly/blob/v0.3.1/zig/pkg/antfly-embedded/src/storage/db/query/search_exec.zig#L13869-L13887) · [Candidate selection](https://github.com/antflydb/antfly/blob/v0.3.1/zig/pkg/antfly-embedded/src/search/search.zig#L1986-L2004) · [Result admission](https://github.com/antflydb/antfly/blob/v0.3.1/zig/pkg/antfly-embedded/src/search/search.zig#L2088-L2124)

The original during-migration traffic shows the same trend across the workload. Grouping 408 variants by question length (26–50, 51–75, and 76 or more words) gives 88, 155 and 165 variants. Within each variant we took the median lexical request time, then took the median across variants in the group. Antfly recorded 156, 200 and 248 ms. The corresponding OpenSearch values were 6.1, 6.9 and 7.6 ms under its original deployment. These are descriptive workload measurements.

To replay the controls, start a dedicated restored release server with `ANTFLY_BENCH_QUERY_PROFILE=1` and `ANTFLY_BENCH_QUERY_PROFILE_EVERY=1`. Supply an API key for the archive reader through `ARCHIVE_PROFILE_KEY_ID` and `ARCHIVE_PROFILE_KEY_SECRET`. Run `python3 lexical_profile.py --endpoint <url> --table <restored-A> --queries <queries.json> --query-id 86:archive:after-schema-change:1 --trace <server.log> --out <new-result.json>` after the restored shard is ready. The runner checks the rank prefix and native filter counts, and writes aggregate measurements. [Replay script](lexical_profile.py)

### Shipped v0.3.2 latency replay

On November 22 we ran both released macOS binaries sequentially on the same local M4 Max. On the restored A index, question 86 took 306.65 ms of median engine time on v0.3.1 and 308.94 ms on v0.3.2. Client medians were 314.90 and 318.49 ms. The ordered top 100 IDs and scores matched exactly. On fresh 4,096-row protected tables, Basic-authenticated lexical p95 was 50.39 and 50.95 ms, respectively. With scoped API keys it was 1.41 and 1.46 ms. Both credentials returned every expected ID in every query. These are targeted serving-only checks, with one trial per release. The full migration p95s above belong to the earlier campaign. [All query arms, credential timings and binary hashes](observations/officeqa-latency-release-replay.json)

### ReleaseFast backport of #1756

A paired native macOS replay compared shipped v0.3.2 with the ReleaseFast v0.3.x build at `7f6031aebd`. On question 86, requesting 100 hits with 59,273 eligible rows, median scoring fell from 302.79 to 37.05 ms and client time from 313.02 to 47.09 ms. All 100 ranked IDs and scores matched. Restricting the result to one eligible ID reduced scoring from 291.59 to 0.18 ms and client time from 295.51 to 4.00 ms. These are nine warm-cache measurements per arm on one query. On the separate protected fixture, lexical p95 with Basic credentials was 49.68 and 50.87 ms, respectively. With scoped API keys it was 1.30 and 1.11 ms. Both credentials returned every eligible row in both versions. [All paired release measurements](observations/officeqa-latency-1756-releasefast.json)

For the workload-wide comparison, restore a private disposable copy of the completed sorted A snapshot for each binary, excluding `metadata/auth` from the copy, then run `latency_backfill.py --database <copy> --binary <binary> --corpus <frozen-corpus> --workload <frozen-workload> --out <new-private-output>`. Each arm replays all 408 evolved hybrid queries at two arrivals/s, first without writes, then during a new B build using 176 batches of 128 frozen rows at one batch per 1.2 seconds. Each stage uses tenant-scoped API keys, checks eligibility on the first 100 hits per channel, and measures client latency from scheduled arrival. The B write target is 22,528 rows. The writer resubmits the same row IDs after a retryable HTTP 429, waits for the server’s advertised delay, and reports retries, terminal errors and indexed row counts. The receipt counts reads completed before the last write acknowledgement, missed arrivals at the concurrency cap of eight, and request failures. Percentiles cover successful scheduled requests. The write rate approximates the earlier encoder migration and fixes the offered load across releases. This protocol measures serving latency during a bounded portion of backfill.

On Wolfspider’s internal SSD, a native trial per binary measured client latency from each scheduled hybrid arrival. All four passes served 408/408 requests. The ordered lists of up to 20 physical pages matched for every query, across builds and across phases. Both B tables indexed 22,528 identical frozen rows, with all reads completed before the last write acknowledgement. [Paired receipt, input and binary hashes](observations/officeqa-1756-backfill-latency.json)

| OfficeQA p95 (ms) | Shipped v0.3.2 | ReleaseFast `7f6031aebd` |
| --- | ---: | ---: |
| Hybrid, serving alone | 410.2 | 252.1 |
| Hybrid, during B writes | 382.8 | 240.0 |
| Lexical call, during B writes | 249.6 | 68.0 |
| Dense call, during B writes | 23.6 | 29.0 |

Two preliminary trials on an external database volume stopped at the client concurrency limit and at a retryable B write rejection, respectively. The paired native-SSD trials used the same 65,536-descriptor limit, recorded missed arrivals and honored retryable write backpressure. Neither final trial missed an arrival or retried a batch. Fixed release-first order and one trial per binary limit run-to-run inference.

## Full PDF deployment comparison

The primary pair is native Antfly and an application-managed PostgreSQL/pgvector deployment with row-level security. Both start with the same versioned PDFs and use EmbeddingGemma 2 for page-image A and text-passage B, the same query vectors, token target and publisher-replay fallback. The image index serves dense queries. The passage index supports lexical, dense and hybrid queries. Each database enforces tenant scope. This measures the two deployments as built. Database internals, transport, producer placement and batching can all contribute to a timing difference.

Both clients run one complete serving-only cycle over the evolved queries, then cycle through them at two scheduled arrivals per second during replacement, with eight in-flight slots. Latency starts at scheduled arrival and includes dispatch delay. Coverage and a scoped B query gate cutover. The controller stops offering A reads and drains outstanding reads before switching. Thus a passing trial demonstrates continued A service during the build and successful B service after the switch. The report records completed, missed, dropped, empty and ineligible reads, cited-page returns, and successful-read p95. Earlier native closed-loop traffic has a separate receipt and cannot supply the paired p95. Use a fresh output cohort for this schedule.

The application-managed Antfly arm remains available for a producer-placement control. OpenSearch, Vespa and LanceDB give separate evidence about retrieval and access enforcement. The PostgreSQL full-corpus reference is a batch producer. The smaller transactional outbox experiment provides the crash/replay evidence.

### Write-path diagnostic

Run the following on an exclusive host with the checkpointed, frozen encoder-migration archive and the pinned native Antfly and LanceDB installations.

```sh
python evaluations/archive/write_batch_sweep.py --binary "$ANTFLY" --corpus "$FROZEN" \
  --workload "$WORKLOAD" --state "$DB_VOLUME/sweep" --out "$RESULTS/write-batches"
```

The first pass alternates Antfly and LanceDB at 128, 512 and 2,048 B rows per call. The second reverses the entire order. Each trial starts from a fresh database, serves A during B writes and checks 616,832 searchable rows plus the B read probe. Per-batch receipts separate Antfly client encoding from HTTP dispatch through decoded committed acknowledgement. The latter includes transport and database time. LanceDB records row projection and its embedded add call, including Arrow conversion. Serving latency and post-ack readiness accompany the batch receipts. Raw timings stay in the artifact.
