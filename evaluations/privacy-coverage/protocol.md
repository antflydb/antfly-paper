# Privacy coverage protocol (draft, not frozen)

Status: draft. Do not measure or report results until this protocol is frozen
with a name, subject commit and pinned inputs, as in
[the retrieval protocol](../retrieval/protocol.md).

## Claim under test

When a privacy policy protects a field, no derived representation and no
external model request contains an original protected value.

## Invariant

Every stored chunk, vector payload, full-text index entry, extracted entity,
cache and log, and every request sent to an external model, is free of the
planted values. Originals may remain only in the protected source field.

## Method

- Corpus with planted personal data (email addresses, phone numbers, account
  and government identifiers) at known offsets, generated so every planted
  value is unique and searchable.
- Producer graph: chunk, embed (through a recording proxy that captures all
  external model traffic), full-text index and entity extraction.
- Inventory: scan storage files, index contents, query results, logs and
  captured model traffic for every planted value and simple variants.
- Repeat with the policy enabled before ingestion and enabled after
  ingestion (backfill), and measure availability during backfill.

## Baselines

- The external pipeline from the head-to-head protocol, with redaction
  applied where its operators would place it.

## Metrics

- Planted values found per location (expected: zero outside the protected
  source for Antfly).
- Detection recall of the redaction model itself, reported separately so
  coverage and detection quality are not conflated.
- Time the table is unavailable during a protective backfill.

## Open questions before freezing

- Planted-value generator and its seed.
- Which storage formats require decoding before scanning.
