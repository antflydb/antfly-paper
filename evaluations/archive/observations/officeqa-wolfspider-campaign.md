# Wolfspider OfficeQA campaign

PostgreSQL, OpenSearch and Vespa run serially as pinned ARM64 containers in a 6-vCPU, 12-GiB Colima VM on Wolfspider. The native macOS client uses the same BGE service as the native trials.

## Publisher-text migrations

| Engine | Rows in B | B ready (min) | Cutover (ms) | A p95 before / during (ms) | Arrivals | Missing / errors | Ineligible | A hybrid recall@20 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| postgres | Run ended without a complete receipt | | | | | | | |
| opensearch | Awaiting receipt | | | | | | | |
| vespa | Awaiting receipt | | | | | | | |

## Raw PDFs and original questions

Awaiting the verified raw corpus manifest.

All arms use the same local Gemma 4 E4B QAT reader, the upstream exact scorer, and identical budgets. Citation ratios are micro-aggregated across questions.

| Context source | Questions | Exact answer | Correct / returned citations | Correct / required citations |
| --- | ---: | ---: | ---: | ---: |
| antfly | Awaiting receipt | | | |
| postgres | Awaiting receipt | | | |
| oracle | Awaiting receipt | | | |

## Real-page graph extraction

Independent real-page graph precision and recall remain pending human adjudication.
