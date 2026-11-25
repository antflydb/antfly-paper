# Document application comparison

Status: **Antfly candidate lifecycle qualification passed**. A fresh-PDF,
real-model run passed all 14 checks, including migration/rollback stored-vector
comparisons and ten rejected invalid controls. See the
[frozen qualification receipt](qualifications/shard-checkpoint-53ae5c8.md).
The exact subject predates the later #1714 readiness optimization. Matched
frozen-v3 qualification and three alternating timing pairs have subsequently
been executed. Comparative performance is not admitted: queries to the old A
index become unavailable while the new B index backfills; see
[Antfly #1716](https://github.com/antflydb/antfly/issues/1716). The lifecycle
qualification and this availability finding are separate results. These runs
do not establish the behavior of the later #1714 build.

The earlier candidate blockers [Antfly #1704](https://github.com/antflydb/antfly/issues/1704),
[#1705](https://github.com/antflydb/antfly/issues/1705), and
[#1706](https://github.com/antflydb/antfly/issues/1706) are resolved. Historical
failures and their immutable receipts remain separate from this successful run.
The candidate build recipe and scope are in
[the native controls](../native-contracts/README.md); this is a distinct cohort
from the released v0.3.0 observations.

Historical released-image diagnosis: with frozen instrument
`622fb9905a77f74df4b56423de01af50bed07a84`, PostgreSQL completed 13 qualification
checks. Antfly passed the four preliminary checks, then timed out during
extractor-backed privacy activation; see [Antfly #1682](https://github.com/antflydb/antfly/issues/1682).
The detector passed the span-bearing positive control. Those historical runs
reused previously extracted fixture text and are not fresh end-to-end timing
trials. Their immutable results, source hashes and not-run lists remain separate.

This implements one controlled permissioned document-search application on
Antfly and PostgreSQL/pgvector with a custom durable outbox. Historical runs
use v0.3.0; candidate runs require the explicit immutable subject manifest
described in [native-contracts](../native-contracts/README.md). PostgreSQL
is designed to succeed: source transactions enqueue durable work; publication
checks current source and policy generations; source deletion cascades; worker
restart rolls back uncommitted jobs; model calls can repeat before commit;
unchanged valid artifacts can be reused. Reads use an actual non-superuser,
non-BYPASSRLS role and RLS. Antfly readers have native authenticated row filters.
The caller sends an unrestricted predicate; the database enforces permissions.

## Scope and equal behavior

The authored Apache-2.0 fixture has eight one-page documents, four born-digital
and four scanned, with text, email, permission, page and relevance annotations
written before model execution. Native text uses PDFium; pages without usable
text use Florence-2. Both architectures run this **same synchronous PDF
frontend before source acknowledgement**, then persist the extracted page text.
The historical `document-v1` correction schedule changes extracted page-text
records. The candidate `document-main-v2` schedule extracts fresh replacement
PDFs for source corrections. Each page is short enough
for one chunk; this is not a multi-page or chunk-boundary quality study.

Antfly manages the extracted text's privacy/embedding/full-text lifecycle.
PostgreSQL's application manages the corresponding lifecycle. Source PDFs and
the detector legitimately receive originals. Neither implementation claims
physical original erasure. In particular, this is **not evidence that table
privacy automatically sanitizes arbitrary PDF-extraction artifacts**. Native
Antfly PDF extraction is a distinct capability and is not credited as eliminating
this application's shared frontend.

The protected boundary checks returned page text, full-text search, and actual
embedding requests. Scanned email transcription errors remain outcomes in
`extraction-quality.json`; an email-shaped OCR variant is still sensitive.
The acceptance check rejects any `@` in protected embedding inputs or returned
text for this fixture. This narrow canary check is not a general PII detector.
Character similarity is a qualification diagnostic, not a broad OCR score.

Models use the same pinned native inference service on both architectures:

- Embedding A: `BAAI/bge-small-en-v1.5`, 384 dimensions.
- Embedding B: `BAAI/bge-small-en`, upstream revision
  `2275a7bdee235e9b4f01fa73aa60d3311983cfea`, 384 dimensions.
- OCR: `antflydb/florence-2-base`, Q4_K GGUF.
- Privacy: the cached `fastino/gliner2.5-base-v1-fp16` weights with extraction
  schema version 2 and explicit UTF-8 span options. A selector-alias diagnostic
  did not establish a fix for the observed native privacy stall.

Every model file is SHA-256 recorded before services start; only selected model
directories are mounted read-only. A manually cached model name is not a
registry-certified revision. The results record actual bytes and do not claim
the provider supplied an immutable identity acknowledgement for these files.
Known synthetic revision-acknowledgement controls live in `lifecycle-extended`.

## Run

Use the locked ARM64 client image built from `../retrieval/Dockerfile` and
`requirements.lock`. Set `PAPER_CLIENT_IMAGE` to its local tag. Materialize the
fixture in that image:

```sh
python evaluations/head-to-head/scripts/fixture.py --out .data/documents/qualification
```

The command above needs PDFium/Pillow, supplied by the client image. Then run
the host controller (Python standard library and Docker CLI):

```sh
python3 evaluations/head-to-head/scripts/run.py \
  --engine antfly --data .data/documents/qualification \
  --models /path/to/model-root --migration-models .data/document-models \
  --out results/document-antfly-01
```

Use `--engine postgres` for the outbox application. Each output path must be new.
Successful owned containers/volumes are reclaimed; failures preserve stopped
state and diagnostics. Private network aliases and loopback host ports are
run-owned. `--extracted` reuses prior extraction only for diagnosis and explicitly
marks the run as not a fresh end-to-end lifecycle. It is not a publication trial.

PostgreSQL readers authenticate as distinct database LOGIN roles without
superuser/BYPASSRLS rights; RLS binds the row tenant to `current_user`, rather
than to a caller-supplied tenant setting. The facade binds credentials to the
reader, ignores a forged tenant field, and retains the policy barrier through
the read. Antfly uses distinct native reader credentials and row filters.

The schedule checks semantic relevance and independent permissions, activates
privacy over existing rows, verifies protected text and model requests, restarts
after publication, holds inference across source correction/deletion and another
restart, restricts permissions, builds B during writes/restart, and creates a new
A rollback generation. Returned vectors are independently recomputed from the
protected result text under the selected model; missing vectors cannot pass.

`--privacy-before-write` is a separate condition with protection installed on
an empty table, not evidence for activating a policy over existing rows.
`--initial-sync full_index` tests the derived-ready acknowledgement separately
from the default asynchronous source acknowledgement. These conditions and
diagnostic extraction reuse must remain visible in results and provenance.

## Serving and backfill

Only after correctness qualification, add:

```sh
--performance --copies 128 --duration 60 --rate 2
```

Use `--antfly-reader-auth api_key` for new Antfly serving measurements. The runner
creates a separate key for each tenant's existing read-only account after
installing its row filter. All tenant reads use that account's key, including
the checks after restart and tenant reassignment. Administrative setup,
writes and readiness polls retain the same Basic login in both modes.
The default, `--antfly-reader-auth basic`, reproduces the historical control.
Both modes record the reader credential type in the result and provenance.
The [v0.3.3 credential comparison](../archive/observations/document-reader-auth-v033.md)
includes the per-arrival data and release-specific commands.

This creates 1,024 controlled page records, **not 1,024 independent natural
documents**. Retire old generations before measurement. Serve A alone, then
serve A while building B. Query embedding is included in end-to-end timing.
The common remote inference service is isolated from the database in both
architectures; this controls placement, rather than crediting Antfly with free
co-location. A co-located variant is not measured by this protocol.

Arrivals are open-loop at the same specified rate. Queueing time, request
errors, unavailable responses and rejected arrivals remain observations. Keep
raw rows and report errors/availability with p50/p95, protected outputs and
relevance. Require backfill completion and report the actual target count and
time; pausing or failing backfill cannot become a latency win. Run three fresh
lifecycles in alternating architecture order. No general throughput or relevance
claim follows from this controlled fixture.

Equal database/application caps are 3 CPUs / 6 GiB total: Antfly 3/6, PostgreSQL
2/5 plus its application worker 1/1. Both additionally receive the same native
model service (2 CPUs / 8 GiB), measurement gateway (0.25 CPU / 256 MiB) and PDF
frontend (1 CPU / 1 GiB while extracting). Model-service host/combined budgets
are 7 GiB with a 3 GiB scratch budget. These are configured caps, **not measured
memory footprints**. Explicit nofile=65536:524288 on database/application
containers is a declared workaround, not a claim that the FD issue is fixed.

The analysis scripts also accept `document-main-v3` results. The pair collector
verifies recorded checksums and replays the frozen independent vector oracle
against retained persisted values and independent expected vectors; it does not
run inference again. It requires all three stable checkpoint witnesses, the
complete eight-document set in each space, and all ten named negative controls.

An application-only campaign may declare `paper-application-pairs-v1`. Its
collector rechecks both fresh qualification results before admitting three
matched timing pairs. This profile establishes application evidence only; it
does not substitute for native-runtime, descriptor, or component benchmarks.
Legacy full campaigns retain their existing component gates. A missing pair,
failed lifecycle, request error, or incomplete backfill prevents comparative
timing admission.

## Counting and limitations

Administrator-only `inspection_witness` and `inspect_vectors` methods are
measurement instruments. List their token lines separately from the Antfly
application adapter; adding a stronger acceptance oracle must not appear as
additional production application code.

Count production services and deployment settings separately from custom code.
`common.py`/`frontend.py` belong to both applications. `adapters.Antfly` is Antfly
application configuration/access code; `adapters.Postgres` plus `pipeline.py`
are PostgreSQL application code. `run.py`, `traffic.py`, `gateway.py`, fixture
generation and tests are measurement instruments, not required product
services. The PostgreSQL HTTP layer is counted because it also owns the custom
outbox application; it is not presented as a database requirement.

LOC is not development time. Record recovery interventions and measured recovery
time separately from deliberate injected actions. Harness setup/compatibility
fixes are not operator repairs of a successful deployed application. Remaining
limits include exact internal response-before-commit failure injection,
on-disk/history/backup privacy audits, broad natural-document quality,
multi-page citations, calls/meetings, and graph workloads.

Research: PostgreSQL's [RLS contract](https://www.postgresql.org/docs/current/ddl-rowsecurity.html),
the [pgvector reference](https://github.com/pgvector/pgvector), and release-tagged
Antfly [document](https://github.com/antflydb/antfly/blob/v0.3.0/docs/guides/index-documents.mdx),
[artifact](https://github.com/antflydb/antfly/blob/v0.3.0/docs/guides/artifact-indexes.mdx),
[privacy schema](https://github.com/antflydb/antfly/blob/v0.3.0/specs/openapi/antfly/privacy.yaml),
and [model-instance API](https://github.com/antflydb/antfly/blob/v0.3.0/specs/openapi/inference/api.yaml).

## Migration fault injection

Index creation validates the embedding provider before accepting a generation.
The migration hold exempts only input digests observed in successful admission
calls during initial index creation, before any source rows exist. These digests
are learned from the subject and do not include model identity, so the second
model can validate while document embeddings remain held. Other hold phases
have no exemptions. The migration crash waits for an audited request with
`hold_applied=true`, rather than treating any pending probe as backfill evidence.
The audit records hashes and hold decisions, never input text. This changes the
instrument; earlier frozen qualification receipts remain immutable.

## Independent stored-vector oracle

Candidate runs use protocol `document-main-v3`; earlier frozen receipts remain
unchanged. Released-image `document-v1` reproduction retains its historical
projection check and does not claim the independent stored-vector guarantee.
Candidate runs always require administrator inspection, without a legacy fallback.

The Antfly vector oracle uses administrator-only raw artifact inspection of
each named index (`dense_index_inspection`, version 1), while tenant serving
queries request only sanitized source fields. The candidate must implement
`antfly.dense-index-inspection.v1`; an absent or unsupported inspection fails
qualification. Ordinary tenant credentials must receive HTTP 403 from the raw
inspection route.

For this eight-document, one-page/one-chunk fixture, the oracle requires an
exact document set and current source tenant, version and page; matches the
inspection's sanitized document to the independently retrieved serving row;
checks policy, generation, recipe and artifact identity; and compares all 384
stored values to a separate native invocation of the intended model on that
serving text. It rejects missing, stale, nonfinite and wrong-model results.
No fallback regenerates an observed vector or substitutes model output for
storage inspection. The deleted temporary document must have no inspection.
Catalog witnesses must remain stable across each bounded comparison. Each
inspection's index incarnation, checkpoint generation and config hash must
match the independent live owner in `shard_status`. Aggregate `status` has no
single physical checkpoint identity. The authored application creates one
shard; acceptance requires exactly one expected, reported, fresh owner with
complete authoritative coverage, and records that owner ID. Missing, stale,
unknown or multiple owners fail closed. Beginning/ending witnesses also detect
owner changes even when the numeric checkpoint identities happen to match.

`vector-inspections.json` retains the captured publication/source/vector
evidence for B, still-serving A, and the newly built rollback A generation.
Receipts are recorded before validation, with expected source/model/live
checkpoint facts, serving rows, verification status and failure reason. When
numeric comparison runs, its independent expected embedding is retained too.
A failing deleted-document check also retains the unexpected snapshot. Every
comparison appends an overall `validation_attempt` receipt with both catalog
witnesses, acceptance or failure, and its reason. Per-document acceptance alone
does not certify the full attempt. Ending witnesses retain incomplete coverage
as an observed state; initial coverage must be complete. A failed ending status read is retained
without hiding the original validation failure.
The oracle deliberately rejects unexpected chunk counts rather than silently
changing the authored fixture's acceptance rule. This is qualification evidence
for the explicit fixture, not a performance or corpus-generalization claim.

Model-free oracle negative controls:

```sh
python3 -m unittest discover -s evaluations/head-to-head/scripts -p 'test_*.py'
```

After B passes, ten deliberate mutations of its accepted real snapshot must
be rejected by the same oracle: wrong model, wrong numeric vector, missing
vector, unpublished state, stale policy, deleted identity and stale source
revision, dirty checkpoint, replay debt and mismatched checkpoint recipe. These controls run before the A-consistency and rollback checks.
