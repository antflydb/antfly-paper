# Crash and update correctness protocol (draft, not frozen)

Status: draft. Do not measure or report results until this protocol is frozen
with a name, subject commit and pinned inputs, as in
[the retrieval protocol](../retrieval/protocol.md).

## Claim under test

Derived state converges to the committed source after crashes, updates and
deletes, and no artifact from an obsolete source version or generation ever
becomes queryable.

## Invariants

- Superseded work never publishes: an artifact derived from source version
  v is never served once version v+1 has committed.
- Deleted sources leave no queryable derived state.
- A crash between a model response and its commit yields each artifact
  exactly once, or leaves it visibly pending.
- Readiness never reports an index complete while upstream stages are
  unfinished.

## Method

- Producer graph: extract text, chunk, embed and extract entities, with a
  deterministic stub model and a nondeterministic generator.
- Fault schedule: crash the process (and, separately, the inference worker)
  after each producer stage and between model response and commit.
- Concurrent workload: updates and deletes to sources whose enrichment is in
  flight, at fixed rates.
- After each fault, wait for readiness, then audit every index against an
  oracle computed from the final committed source.

## Baselines

- The external pipeline from the head-to-head protocol, with publication
  conditioned on source version.

## Metrics

- Obsolete or orphaned artifacts observed at any point (expected: zero).
- Duplicate artifacts after recovery.
- Time to convergence after each fault.
- Manual repair steps needed, if any.

## Open questions before freezing

- Fault injection mechanism for each implementation.
- Oracle definition for nondeterministic producers (recorded decisions).
