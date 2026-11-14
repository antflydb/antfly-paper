# Revision fencing and supported generation migration

Status: draft workload expansion. `lifecycle-v1` is the separately frozen
synthetic subset described in [lifecycle](../lifecycle/README.md). A draft
model-performance experiment is never reported as completed evidence.

## Contract

Use supported new indexes, fields or tables for incompatible embedding spaces.
A0, B1 and A2 are distinct generations, even though A0 and A2 use the same
encoder. A serving client chooses a generation and its matching query encoder
as one configuration. This does not assert a global atomic alias transaction.
Every query's provenance must identify its generation and query model.

## Comparisons

- Antfly v0.3.0 official image, explicit named replacement indexes and actual
  queryable/complete coverage signals.
- PostgreSQL with versioned artifact rows, source-conditioned publication and
  durable work tracking. Name an actual workflow engine when one is used;
  a custom outbox worker is labeled and counted as custom code.
- LanceDB Embedded: supported embedding functions and merge_insert into
  separate tables. Synchronous source acceptance is compared with a derived-ready
  write, not a separately acknowledged asynchronous source commit.
- OpenSearch: separate semantic fields and ML Commons connectors and supported endpoint
  compatibility, task/refresh/readiness and application cutover procedure.
- Vespa: separate synthetic embedding fields with pinned model configuration,
  configuration convergence, stored-data reindexing and query cutover. Keep
  its execution explicitly opt-in; documentation is not a measured result.

## Schedule and observations

Ingest A0; keep querying it while building B1. Update source documents during
the build, observe the latest expected vectors independently, then select B1
and its encoder. Repeat into A2. Hold old provider replies across source
updates and cutovers. Include crash/restart during migration and incomplete
upstream work. Avoid holding a provider's schema-validation call as if it
were corpus backfill. A provider call barrier proves that request is in flight,
not the exact response-before-publication instant.

Run a separate alias-mutation and wrong/missing-revision-ack control against
explicitly pinned identities. Equal vector dimension prevents dimension
validation from standing in for model identity. An independent deterministic
provider makes fixture revision and source fingerprint observable; real
pinned encoders are a separate performance/relevance experiment. Provider
attestation tests assume the provider reports its identity truthfully.

## Metrics and acceptance

Report incompatible-space hits, published obsolete work, post-barrier stale
results, errors/unavailability, coverage, build/cutover/recovery time, producer
calls and coordination code. A zero-violation claim requires nonempty query
samples before/during/after each transition and successful baseline reads;
empty or failed queries do not pass safety vacuously. Asynchronous visibility
of an earlier committed representation is reported as lag until the declared
barrier, not automatically a violation. Failures, deadlines and unsupported
checks remain explicit; no silent fallback to external vectors.
