# OfficeQA hybrid-search latency on Antfly #1814

Wolfspider (Apple M4 Max, 36 GiB RAM, macOS 26.6.2) ran uninstrumented Zig 0.17.0 ARM64 ReleaseFast builds of the immediate parent and the merged commit in two reverse-order pairs. Both targeted aarch64-macos.15.0 with the baseline CPU. Each run started from a copy-on-write restore of the same 616,832-record snapshot, issued the same four warm-up queries, and let the server settle for 30 seconds. The client offered two tenant-scoped native hybrid queries per second, with 100 candidates per channel and up to 20 distinct pages after fusion. Each phase contained 408 queries. During backfill, the client offered 176 batches of 128 records to a separate table at 1.2-second intervals. The serving phase always came first.

| Workload | Parent p95 HTTP (ms) | #1814 p95 HTTP (ms) | Change |
|---|---:|---:|---:|
| Serving only | 120.84 | 93.94 | -22.3% |
| During backfill | 101.74 | 83.76 | -17.7% |

Percentiles use the nearest rank over the two runs per build (816 requests per phase). HTTP latency runs from dispatch through response decoding. The scheduled end-to-end metric also includes any client dispatch delay and result handling.

| Run | Build | Serving HTTP p95 (ms) | Backfill HTTP p95 (ms) | Serving scheduled p95 (ms) | Backfill scheduled p95 (ms) | Reads finished before final write | Write retries |
|---:|---|---:|---:|---:|---:|---:|---:|
| 1 | after | 94.68 | 85.57 | 116.81 | 108.82 | 408/408 | 26 |
| 2 | before | 122.05 | 96.44 | 138.30 | 116.19 | 408/408 | 0 |
| 3 | before | 112.61 | 105.04 | 134.75 | 128.26 | 408/408 | 1 |
| 4 | after | 90.43 | 81.75 | 112.49 | 101.55 | 408/408 | 3 |

### Result parity and workload checks

- Serving only, using the first run as reference across 1,224 query comparisons, had 0 ordered ID differences, 0 score differences, and 0 top-page differences.
- During backfill, using the first run as reference across 1,224 query comparisons, had 0 ordered ID differences, 0 score differences, and 0 top-page differences.

The [aggregate JSON](officeqa-hybrid-1814.json) records each binary digest, frozen input digests, query and write completion, and per-run percentiles. This experiment measures end-to-end latency with query profiling disabled. Internal retry counts require a separate diagnostic.

Parent and merge commits are recorded below.

- Before 0e6af23c365b337a84342a3bea89513ec9ec6b55. [Commit](https://github.com/antflydb/antfly/commit/0e6af23c365b337a84342a3bea89513ec9ec6b55)
- After be3a878d4008df9b4545b05daa9eb6117cc3c029. [Commit](https://github.com/antflydb/antfly/commit/be3a878d4008df9b4545b05daa9eb6117cc3c029)


### Reproduction

Build each pinned commit from its `zig/` directory using Zig 0.17.0 and `zig build antfly --release=fast -Dtarget=aarch64-macos.15.0 -Dcpu=baseline`. Follow the [sorted snapshot and load protocol](../COMPARABILITY.md). Run [latency_backfill.py](../latency_backfill.py) with `--native-hybrid --binary "$BINARY" --database "$FRESH_DATABASE" --workload "$WORKLOAD" --corpus "$CORPUS" --out "$NEW_OUTPUT"` for each of the four runs. Use a disposable restore for every invocation and provision fresh benchmark credentials. Keep profiling disabled. Compare the three per-query digests at the same phase and query ID and pool HTTP durations by build and phase before taking the nearest-rank p95.
