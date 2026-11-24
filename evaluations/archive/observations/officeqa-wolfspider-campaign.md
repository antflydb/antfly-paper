# Wolfspider OfficeQA campaign

PostgreSQL, OpenSearch and Vespa run serially as pinned ARM64 containers in a 6-vCPU, 12-GiB Colima VM on Wolfspider. The native macOS client uses the same BGE service as the native trials.

## Publisher-text encoder migration

| Engine | Rows in B | B ready (min) | Cutover (ms) | A p95 before / during (ms) | Arrivals | Missing / errors | Ineligible | A hybrid recall@20 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| postgres | Original readiness probe failed | | | | | | | |
| opensearch | 616,832 | 93.02 | 16 | 192 / 160 | 11,163 | 0 / 0 | 0 | 20.37% |
| vespa | 616,832 | 122.95 | 16 | 201 / 159 | 14,754 | 0 / 0 | 0 | 15.43% |

The table covers the original Linux-VM cohort. PostgreSQL returned no lexical hits on the first evolved readiness query and did not complete that gate. A separate run used a known-positive query (ordinal 284), verified 616,832 B rows in 103.20 minutes and recorded zero missing arrivals, query errors or ineligible pages. The native macOS Antfly and LanceDB trials used a shared application-side embedding worker and client cutover. [PostgreSQL follow-up](officeqa-matched-followups.md) · [Native trials](../REPORT.md#work-completed-during-migration) · [Probe selection and deployment boundaries](../COMPARABILITY.md)

Full receipt SHA-256 digests

- opensearch `638e427c1f48d606011c8b245a31c2619c9a596957d150b234ea81ef9fd12a10`
- vespa `1a24ca891ad1fe7ac60317957d95da92554694fc94d09106a6735546bb8d1a54`

## Raw PDFs and original questions

The verified OfficeQA Pro v2 inventory contains 1,435 PDFs, 118,524 physical pages and 90 questions with 823 distinct question–page citations. The original-question retrieval experiment measured hybrid page recall@20 of 14.17% for Antfly and 14.06% for LanceDB over publisher text. End-to-end exact-answer scores for Antfly, PostgreSQL and the oracle remain unreported. [Corpus verification](corpus-verification.json) · [Original-question retrieval receipt](officeqa-original-recall.json)

## Page-image to passage migration

As of November 24, 2026, the v0.3.3 campaign on Wolfspider was preparing image vectors and text passages and building the native raster lookup. Five application-managed engine trials and a separate Antfly-native PDF trial are queued after preparation. Full-corpus build times, cutover measurements and retrieval scores for this experiment are pending. OCR fallback uses pre-extracted publisher text on the full corpus. Real LightOnOCR measurements come from separate, smaller runs. [Protocol and checksums](../README.md#publisher-replay-and-measured-raw-pdf-sample) · [Fixture and sample results](../REPORT.md#page-image-to-text-chunk-migration)

## Real-page graph extraction

Independent real-page graph precision and recall remain pending human adjudication.
