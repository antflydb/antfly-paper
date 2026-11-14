# Revision fencing protocol (draft, not frozen)

Status: draft. Do not measure or report results until this protocol is frozen
with a name, subject commit and pinned inputs, as in
[the retrieval protocol](../retrieval/protocol.md).

## Claim under test

An index never serves vectors from two embedding models at once. During a
model change, queries are answered entirely from the old generation until the
new generation is complete, then entirely from the new one.

## Invariant

For every query, all returned vectors and scores come from exactly one
model revision. A query that observes a mix of revisions is a failure.

## Method

- Subject: a pinned Antfly release, with two embedding models A and B of the
  same dimension, so a dimension check alone cannot catch mixing.
- Ingest a fixed corpus under model A until the index is complete.
- Switch the producer to model B while continuously querying and while new
  documents are written, then switch back to A before B completes.
- Each result carries the model revision that produced it, recorded by the
  harness independently of the engine where possible (for example, by
  re-embedding returned items with both models and checking which space the
  score matches).
- Repeat with a crash during each phase of the switch.

## Baselines

- The external pipeline from the head-to-head protocol, using its own
  versioning convention.
- Engines whose model change is a configuration update of the same index,
  where the platform permits it.

## Metrics

- Queries observing a mixed space (expected: zero for Antfly).
- Time from switch request to a complete, servable new generation.
- Queries answered during the switch, and their recall against the
  generation they were served from.

## Open questions before freezing

- Corpus and model pair, with weights pinned by hash.
- How revision provenance is attested for engines that do not report it.
