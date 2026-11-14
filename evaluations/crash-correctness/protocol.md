# Crash, update, deletion and replay correctness

Status: draft expansion. The initial executable `lifecycle-v1` subset uses
synthetic remote embeddings; see [its exact coverage](../lifecycle/README.md).
Do not infer heterogeneous, distributed or precise internal-failpoint coverage
from a successful vector-only process-restart test.

## Invariants by consistency mode

Superseded in-flight work cannot overwrite the currently published source
version. A derived-ready acknowledgement is a publication barrier: subsequent
checks must match that acknowledged source and generation. Under asynchronous
source acknowledgement, earlier published derived state may remain visible
while replacement catches up; measure this lag separately. Every mode must
converge, expose outstanding/failed work, and avoid deleted-source resurrection
after the declared deletion/readiness barrier.

Exactly-once publication is an effect invariant, not an exactly-once model-call
claim. Before publication, retries may invoke a model again. A committed
nondeterministic decision must retain its recorded value across restart and
identical writes; provider call counts distinguish reuse from accidental
reproduction.

## Schedule

Use deterministic producer barriers and an independent source ledger. Include
source update and deletion while an old response is held; a process crash
with a request held; restart with the old response released; metadata-only
updates; provider failure, retry and visible repair debt; and read-your-writes
checks. Exercise source→extraction→chunk→embedding dependencies and generation
migration as separate declared conditions. Every claimed producer family must
actually execute. Compare native managed producers with a correctly fenced,
recoverable external pipeline using the same source history and provider.

A network barrier does not identify the instant between model response and
artifact commit. Only an instrumented subject failpoint can qualify that
boundary; mark it not-run until implemented. Likewise distinguish whole-server
restart from an isolated inference-worker crash. Test committed-decision replay
only after independently reading the artifact back from the database.

## Measurements

Invariant violations and checked observations, duplicates/orphans, recovered
artifact values, outstanding jobs, retries/model calls, time to convergence,
readiness accuracy, errors/unavailability and manual repair steps. Timeouts
are incomplete or failed checks, not zeros. Retain failed owned state and
checksum-verified summaries; keep raw source/prediction traces out of records.

Pin release/image, provider fixture, source ledger, sync mode, deadlines and
fault schedule before running. Use real models for latency, never synthetic
provider timings as inference performance. Tests must reject an empty sample
set, an always-unavailable engine and a deliberately stale adapter.
