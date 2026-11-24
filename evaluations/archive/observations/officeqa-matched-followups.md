# Matched OfficeQA follow-ups

The PostgreSQL trial includes fresh B inference and keeps the original full-question lexical parser. Its supplemental disjunctive replay runs after timing. The Linux controls use frozen B vectors, the same 4-CPU/4-GiB server limits, and two opposite trial orders. Each arm uses evolved workload probe 284. The [comparison audit](../COMPARABILITY.md) records how that probe was selected.

| Trial | Rows | B ready (s) | B write calls (s) | B model calls (s) | A scheduled / lexical / dense p95 (ms) | Missing / errors / ineligible | A hybrid recall@20 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| PostgreSQL | 616,832 | 6192.10 | 653.88 | 5491.65 | 418 / 272.41 / 23.05 | 0 / 0 / 0 | 11.11% |
| 1 opensearch_policy | 616,832 | 259.54 | 219.89 | 0.00 | 175 / 32.2 / 132.73 | 0 / 0 / 0 | 20.35% |
| 1 antfly_policy | 616,832 | 451.58 | 402.78 | 0.00 | 471 / 295.91 / 223.86 | 0 / 0 / 0 | 22.52% |
| 1 opensearch_trusted | 616,832 | 255.19 | 218.57 | 0.00 | 128 / 21.98 / 111.84 | 0 / 0 / 0 | 19.96% |
| 2 opensearch_policy | 616,832 | 262.61 | 223.16 | 0.00 | 154 / 30.87 / 110.5 | 0 / 0 / 0 | 20.32% |
| 2 opensearch_trusted | 616,832 | 254.39 | 218.06 | 0.00 | 214 / 25.19 / 192.79 | 0 / 0 / 0 | 19.95% |

PostgreSQL any-lexeme A hybrid recall@20 was 9.65%. Its lexical channel returned no pages for 0 evolved variants.

Full receipt SHA-256 digests

- PostgreSQL `436b82dd8a6adc51f308589be2ecc0c42ac7ccd4e0a4d453ff937d9ec7c84bc9`
- 1 opensearch_policy `d3c0551dbe294dc88ce19d07ae13cd41d4fd8ec6ab112019aacf674679231945`
- 1 antfly_policy `770a82533e486365318ea1871adf1194520bb411c28e41d98740451565e22a1a`
- 1 opensearch_trusted `0b75f516d9cfbb8c4fec5ad4bbfe27c4777dd7bca8419d045bf799a9bfe5330b`
- 2 opensearch_policy `080bc24672ef3fa4cd4758ba36e0da1a858b927be13c610e8f6c07a55b2afaf5`
- 2 opensearch_trusted `03742af65a3c92fe6702bef2f7a09edf58d3c13bd59673f08e026c5761aa0c5a`

Frozen-vector trials without complete receipts: 1 antfly_basic, 1 antfly_trusted, 2 antfly_basic, 2 antfly_policy, 2 antfly_trusted.
