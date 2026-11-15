# Head-to-head at equivalent useful behavior

Status: draft. First build and validate one complete permissioned document
application; calls/meetings and graph applications follow as breadth studies.
No comparison is completed because an acceptance manifest or adapter exists.

## Architectures

- Antfly managed producer graph and server-owned policies.
- PostgreSQL with pgvector/ParadeDB where useful, actual RLS and durable
  orchestration, versioned artifacts, dependency completion and conditional
  publication. A custom outbox implementation is identified as such, never
  called Temporal/Dagster. A workflow-engine variant must run the actual engine.
- LanceDB Embedded supported synchronous embedding/merge-insert path and a
  trusted application enforcing policy. Add workers only for actual background
  work; the SDK measurement facade is not a required product service.
- OpenSearch semantic fields and ML Commons connectors on a pinned supported release/license;
  Vespa's feed-time models, reindexing and new-field cutovers are a separate
  explicitly opted-in reference. Do not treat either as an external-vector sink.

## Applications and acceptance

1. Permissioned born-digital/scanned document search: OCR fallback, chunk
   coordinates, redaction, semantic/full-text retrieval, source correction,
   deletion, model migration and restart. Start with a small licensed fixture
   with independent text, PII, ACL, citation and relevance annotations; scale
   only after all required behavior works.
2. Call/meeting search: timestamps/speakers, fixed/pause/speaker-boundary
   ablations with identical models/budgets. Human diarization isolates boundary
   effects; automatic diarization is a separate end-to-end condition.
3. Agent graph retrieval: extracted edges, hybrid ranking and graph expansion,
   authorization at every claimed boundary. Compare the strongest supported
   peer same-engine joins too; do not force separate services unnecessarily.

Use the paper's event sequence: write, protective policy, new-model build,
permission restriction, source correction while work is held, crash/recovery.
Measure both async source-acceptance and derived-ready paths with like contracts.
The external implementation is designed to pass and reviewed before timing.
Quality/behavior qualification precedes a cost or latency ranking.

## Counting and controls

Count required production services, failure domains, custom lifecycle code,
configuration, deployment/upgrade/repair actions and observed operator time.
Exclude generated code and common fixture/measurement code, but classify all
code transparently. LOC is not developer effort. Count Antfly's remote model
service/worker just as for peers. Share models/caches where the supported
architecture permits it; do not force redundant model copies.

For composition, compare identical candidates/fusion/edges/fanout and result
semantics, with Antfly composed versus staged-client execution and strongest
supported peer plans. Report bytes moved, requests, p50/p95 and correctness
under updates. For interference, open-loop arrivals and fixed offered load:
serving alone, serving+backfill and isolated inference at equal total resources
and identical models. Report query tails/errors, queue growth, throughput and
completed backfill work; pausing backfill is not a free serving win.

Pin corpora licenses/hashes, models, APIs, index definitions, sync modes and
acceptance checks. Quantitative storage/recomputation and typed-decision claims
need matched-quality controls including original source/model bytes or the same
batched local classifier. Synthetic lifecycle fixtures cannot substitute for
real-model quality or throughput. Missing results remain not-run.

## Main candidate condition: document-main-v2

A pinned-main manifest selects `document-main-v2`; historical `document-v1`
results retain their original interpretation. Both application architectures use
the same candidate native inference image and the same hashed model bytes.
Fresh trials extract all eight PDFs and replacement PDFs at source versions 2,
3 and 5. Version 4 changes permissions over the preceding source. Controlled
scaling copies those authored pages and does not constitute a natural-corpus
quality result. Reusing extracted text is diagnostic only.

The source-acceptance path uses asynchronous writes in both architectures;
ready-state observations and explicit `full_index` barriers are separate. A
held deletion uses source-write acknowledgement, permitting the held-work
experiment to proceed. Standalone native-contract controls measure the new
`commit_requires: direct_enrichments` and atomic alias API; this application's
explicit named-generation queries do not themselves establish alias cutover.

Record time from process kill to recovered useful behavior, injected actions,
and inference calls. These are harness recovery observations, not measured
human operator effort. For backfill, record actual ready-state completion time
separately from the fixed serving window and its combined wall time. Retain
all arrivals, errors and unavailable results before calculating latency.

Qualification must pass on both architectures before comparative timing. Run
three fresh trials per architecture in alternating order, with 128 copies of
each of the eight fixture pages (1,024 pages), 60 seconds serving alone and 60
seconds serving during backfill at 2 offered queries/second. Preserve per-service
CPU/memory limits and shared-inference placement across both architectures;
record those limits and the client image ID in every trial. Report failed
qualification separately and do not pool main-candidate results with v0.3.0.
