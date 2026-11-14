# Native lifecycle baseline API notes

Research snapshot: 2026-11-13 local time. Runtime versions are pinned in
subjects.json; rolling documentation is design guidance and executed probes
establish the pinned API. No documentation-only property is a measured result.

## Vespa 8.781.11

Use the shipped `openai-embedder` (available since 8.678) with a private
OpenAI-compatible provider and explicit dimensions. It requests base64-encoded
little-endian float embeddings; the provider honors encoding_format. This
executes Vespa's feed-time embedder, not external precomputed vector insertion.
Schemas use derived synthetic fields. Deploy, wait for service convergence,
mark reindexing ready, redeploy, wait for `clusters.content.ready.lifecycle.state`
to become successful and independently verify all expected returned vectors.
Readiness is not inferred from an HTTP 200 or an empty corpus.

Native reindexing visits stored documents, recomputes indexing outputs, reports
progress, resumes failed work and yields resources. A new-field rebuild plus
query switch is explicitly documented. Do not label these mechanisms absent.
The current fixture uses synchronous feeds and a whole-container restart, not
an isolated inference-worker crash or a separately acknowledged async source.

- https://docs.vespa.ai/en/reference/rag/embedding.html#openai-embedder
- https://docs.vespa.ai/en/operations/reindexing.html
- https://github.com/vespa-engine/vespa/releases/tag/v8.781.11

## OpenSearch 3.9.0

Use native semantic fields with a registered/deployed ML Commons remote model,
the standard OpenAI embedding connector preprocess/postprocess functions, and
new model IDs/indexes for A0/B1/A2. A named model ID is not automatically a hash
attestation to externally mutable weights; test each contract separately.
Semantic mappings include companion embedding/model metadata. Supported
unchanged-input reuse is a field setting, not an assumed exclusive Antfly
capability. Scalar-only updates avoid artificial source re-embedding.

The synthetic provider uses no real key. The connector API still requires a
credential object; supply the explicitly synthetic value. Only the owned cluster
enables private IP inference and its private endpoint regex matches exactly
`http://provider-[ab]:8090/v1/embeddings`. No public model host is allowlisted.
These cluster-local settings never modify an existing operator installation.

- https://docs.opensearch.org/3.9/mappings/supported-field-types/semantic/
- https://docs.opensearch.org/3.9/ml-commons-plugin/remote-models/index/
- https://docs.opensearch.org/3.9/ml-commons-plugin/remote-models/blueprints/
- https://github.com/opensearch-project/ml-commons/blob/main/docs/remote_inference_blueprints/ollama_connector_chat_blueprint.md
- https://github.com/opensearch-project/OpenSearch/releases/tag/3.9.0

## LanceDB 0.41.0 and PostgreSQL outbox

LanceDB uses its actual EmbeddingFunction registry, SourceField/VectorField,
add/merge_insert and metadata-only update. Calls happen synchronously before
acceptance; compare with a derived-ready path. Private facade code is test
instrumentation. Do not call it a mandatory separate production service or
claim native Embedded RLS from a correctly supplied predicate.

The PostgreSQL application commits source and pending jobs transactionally;
its worker locks a job, calls the provider, then locks/checks current source
before publishing. Source deletion cascades artifact deletion, with held old
jobs allowed to complete as superseded. Job transactions roll back on worker
crash. Revision checks, retries, readiness and generation cutover are explicit
application coordination. This is a viable custom outbox, not a measured
Temporal/Dagster implementation or a minimal-code claim.

- https://lancedb.github.io/lancedb/python/python/
- https://www.postgresql.org/docs/18/explicit-locking.html
- https://www.postgresql.org/docs/18/sql-select.html
