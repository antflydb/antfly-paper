# Retrieval protocol: paper-default-v030-v3

Freeze the subject, inputs and configuration before measurement. Changes
require a new attempt; retain failures and exclude superseded attempts from
the primary repeated-run comparison.

- Antfly v0.3.0, source commit `727b70d1e22714a11a1b6a06ede48c435241237c`.
  Default profile: omit search_effort; effective server default 0.5.
  Calibration is informational and cannot replace this configuration.
- PostgreSQL/pgvector 0.8.7 shared and partial HNSW indexes; ParadeDB 0.26.6
  native vectors; LanceDB Embedded 0.41.0 native prefiltering. Image and
  dependency hashes are pinned by the runner and lockfile. PostgreSQL RLS
  uses fixed non-owner login roles; Antfly uses authenticated server row
  filters. LanceDB authorization is supplied by the trusted application.
- Public [VectorDBBench Cohere medium corpus](https://assets.zilliz.com/benchmark/cohere_medium_1m/):
  1M rows, 768-dimensional float32 vectors, cosine similarity, top 10.
  Pin downloaded files by SHA-256. Canonical IDs are parquet row ordinals.
- Eight permission sets: seeded random and fixed-anchor-correlated sets at
  50%, 10%, 1%, 0.1% eligibility. Correlation is to the declared anchor, not
  each query. Ground truth is independent float64 eligible-set cosine rank.
- Twenty calibration and 100 disjoint holdout queries. Peer tuning selects
  the lowest calibration median among settings with recall >=0.955,
  complete authorized results, zero errors and zero duplicates. No choice
  may consult holdout. The main Antfly setting is predeclared regardless of
  calibration outcome. Report any holdout qualification failure explicitly.
- Three fresh index lifecycles per profile with rotated profile order.
  Engines run sequentially, each with 4 CPU / 12 GiB; the separate client
  has 1 CPU / 1 GiB. Allow 30 seconds after indexing, then 20 calibration-only
  warmup queries before each primary holdout case. No rebuild after tuning.
- Measure all primary holdout cases before diagnostics. Primary acceptance
  requires mean recall@10 >=0.95, ten authorized unique results, no request
  errors or unauthorized hits. Only passing repeated results support
  matched-quality latency comparisons. Report per-lifecycle p50/p95 and
  their mean/range rather than pooling away variation.
- Eligible-set exact diagnostics use the first **five** predeclared holdout
  queries per permission set. They validate the implementation; their small
  sample is not a headline latency comparison. Primary holdout remains 100.
- The global exact control runs all 100 holdout queries once per lifecycle.
  Immutable results are reused across eight offline permission views. Failures
  are retained without retry. Report source timing/errors once, and derived
  completeness/recall separately. Derived views have no separately timed
  latency. The top-ten postfilter control is a stated starvation ablation,
  not a claim about every external pipeline or oversampling strategy.

Queries use persistent remote API connections and finish timing after client
response decoding. PostgreSQL uses its native wire protocol; Antfly uses
JSON HTTP. LanceDB runs behind a private SDK measurement facade in the engine
budget, with a separate HTTP client. The facade is instrumentation, not a
required LanceDB product service or native RLS claim.

PostgreSQL builds use 6 GiB maintenance_work_mem with 8 GiB shared-memory
capacity within the 12 GiB engine limit. HNSW m=16, ef_construction=200;
query work_mem=64 MiB. See [upstream build guidance](https://github.com/pgvector/pgvector#index-build-time).
Native ParadeDB settings are those exposed by its pinned release, not
potentially newer rolling documentation. Candidate grids are in adapters.py.

The 10k wiring fixture uses two calibration and three holdout queries and
does not qualify publication results. Atomic per-query progress and aggregate
checkpoints are written outside query timers. Error classes are logged
immediately. No predictions, vectors or credentials belong in retained
aggregate summaries. Request timeouts remain 120 seconds; the controller
allows six hours per lifecycle and retains stopped diagnostic state on errors.
