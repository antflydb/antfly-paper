# Head-to-head protocol (draft, not frozen)

Status: draft. Do not measure or report results until this protocol is frozen
with a name, subject commit and pinned inputs, as in
[the retrieval protocol](../retrieval/protocol.md).

## Question

Both architectures can maintain the paper's invariants. What does each need
in coordination code, operating effort, recovery work and interference to do
so, and where does database ownership cost more?

## Implementations

- **Antfly:** a pinned release, with producer graphs declared through its API.
- **External pipeline, built to succeed:** PostgreSQL with pgvector and
  row-level security, plus a durable workflow engine (Dagster or Temporal)
  with lineage, versioned artifacts and publication conditioned on the source
  version. Built by engineers instructed to meet every invariant, and reviewed
  before measurement.

## Applications

1. Call and meeting search with speaker turns and timestamps.
2. Scanned and born-digital document search with OCR fallback and redaction.
3. Permission-scoped agent retrieval over an extracted knowledge graph.

## Scenario

Drive each application through the events in the paper's invariants table: a
write with enrichment, a privacy policy, an embedding-model upgrade,
a permission restriction mid-upgrade, a correction while work is in flight,
and a worker crash. Check each invariant after every event.

## Metrics

- Invariant violations (both implementations may reach zero).
- Coordination code, counted with published rules; generated code excluded.
- Services operated, separately monitored failure domains, deployment steps.
- Manual repair procedures and recovery time after each fault.
- Query latency interference while enrichment runs.
- Where ownership costs more: rebuild time and cost after a producer change,
  and the coupling of pipeline changes to database releases.

## Open questions before freezing

- Corpora for each application, with licenses.
- Counting rules for coordination code and configuration.
- Review process for the external implementation.
