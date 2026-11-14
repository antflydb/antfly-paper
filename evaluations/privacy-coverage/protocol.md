# Protected downstream privacy coverage

Status: draft; synthetic lifecycle controls and real detector accuracy are
separate experiments and must have separately frozen input manifests.

## Policy boundary

The policy prevents original sensitive values from reaching protected downstream
consumers after its activation barrier. Antfly's current redact-for-search-and-AI
mode retains originals. The privacy detector necessarily sees the original;
record its placement/trust boundary separately from downstream embedders and
models. Graph consumers refused under protection are unsupported, not passing
sanitized graph output. Physical erasure from old generations/backups is a
separate retention contract.

## Workload and controls

Generate unique synthetic emails, phone/account identifiers and their known
positions. Use a perfect controlled detector first to isolate policy coverage;
then evaluate a pinned real detector against independent annotations, including
false negatives/positives and quality of sanitized retrieval. Same detector,
policy, source and model on every stack. The external baseline places redaction
before its protected consumers and may succeed.

Exercise policy-before-ingest, activation/backfill on existing data, concurrent
source changes, deletion, detector outage/restart, retries and held old replies.
Observe source and derived-ready barriers independently. Inventory protected
chunks, text, vectors' source/payloads, enrichment fields, query paths and model
requests. Raw vector bytes cannot prove their input was sanitized: intercept
the embedder input and independently check its expected vector.

Decode compressed/structured storage when claiming an on-disk inspection.
Report original stores, detector requests, logs, caches, retained generations,
snapshots and backups with separate verdicts: clean, expected original,
violation, inaccessible or not-inspected. A grep miss is not proof that a
compressed copy or every storage surface is clean. Observation failures fail
closed in the harness rather than being called clean.

## Metrics

Protected downstream exposures by surface, detector quality, model-call/retry
counts, policy/backfill completion, unavailable time and repair effort. Report
sampling/coverage limits. Do not call current-row deletion physical erasure.
Pin detector, fixture, exact policy, consumer graph, supported API and edition
before measurement; never send planted or real data to a paid service by default.
