# Versioned financial archive

The public fixture, five retrieval adapters, two graph adapters, source generator, annotations, acquisition verifier and scorers live in this repository. The corpus arm uses the gated [Databricks OfficeQA Pro v2 release](https://huggingface.co/datasets/databricks/officeqa-pro-v2). Keep corpus files, answer keys, credentials and model output outside Git.

## Reproduce the public qualification

From the repository root, on Linux ARM64 or Docker Desktop ARM64:

```sh
python3 -m unittest discover -s evaluations/archive -p 'test_*.py'
python3 evaluations/archive/qualify.py --out /tmp/archive-qualification
```

The controller builds the locked client from `evaluations/retrieval/Dockerfile`, runs each pinned engine serially in its own disposable container or embedded directory, and removes the containers on exit. Supply a fresh output directory. To include actual OCR of the versioned HTTP objects, add `--ocr-weights /path/to/antflydb/florence-2-base`. The directory is mounted read-only into the release inference service. The [document application](../head-to-head/README.md) identifies the model and model-file audit. The dependency-free graph judgments and tiny matched vectors are authored for this paper. Three source pages include two versions of the same scan. Fixture timings are diagnostics.

The matched retrieval runner builds an A index, exercises the initial tenant schema, adds integer retention years, replays queries and builds a separate physical B index under A traffic. Each engine returns lexical and dense chunk ranks. The common hybrid calculation adds reciprocal ranks with constant 60, breaks ties by chunk ID and collapses chunks to distinct physical pages. The qualification requires the expected top page in all scored cells, zero unauthorized returned pages and no missing scheduled arrivals. The original unfiltered questions use a separate logical reader and a public copy of each chunk. Physical row counts include public and tenant copies. Query embeddings in this arm are precomputed.

| Engine | Schema and serving path | Tenant enforcement |
| --- | --- | --- |
| Antfly v0.3.1 | Flexible documents, full text and external vectors, default search effort | Separate authenticated readers with native row filters |
| PostgreSQL + pgvector | JSONB metadata with expression indexes, generated tsvector with GIN, HNSW with iterative scan | Distinct non-owner login roles under RLS |
| OpenSearch | Explicit typed mappings, BM25, filtered Lucene HNSW | Trusted adapter supplies the filter |
| Vespa | Typed application schemas, BM25 and nearest-neighbor rank profiles | Trusted adapter supplies the filter |
| LanceDB Embedded | Arrow schema evolution, native FTS, cosine vector search, IVF_FLAT for larger tables | Trusted SDK application supplies the prefilter |

The graph arm reads the same PDFs through a shared frontend. It checks a strong ETag before accepting bytes, rejects a stale conditional GET on replacement, routes scanned pages to Florence and born-digital pages to PDFium, and extracts the authored payment relations with a small deterministic vocabulary. Hybrid retrieval seeds a two-hop native Antfly graph query or indexed PostgreSQL joins. The page-level oracle checks two independent supports, correction and revocation at an intermediate vertex for two authenticated readers. Each reader's complete returned set must agree with the oracle. The frontend owns source-version changes in this arm. The database owns traversal and access control. Separate native [alias and write-barrier](../native-contracts/README.md) and [durable outbox](../head-to-head/README.md) instruments exercise generation pinning, held responses, crashes and replay.

## Prepare OfficeQA

Use Python 3.11+ and the locked evaluation client dependencies. A previously verified acquisition works without any credential. Starting with an empty directory requires an approved Hugging Face account.

```sh
cd evaluations/archive
python3 workload.py download --data "$DATA"
python3 workload.py compile --data "$DATA" --out "$WORKLOAD"
python3 corpus.py --data "$DATA" --workload "$WORKLOAD" --out "$CORPUS" \
  --models "$MODEL_URL" --weights "$MODEL_DIRECTORY"
```

The model endpoint serves the BGE A and B models specified in the document application. Point the weights option at the same mounted files. A completed per-document checkpoint includes its output SHA-256. Use the same command with `--resume` to continue an interrupted preparation. Readers require the final manifest. The compiler checks the original dataset revision, file inventory and checksums, question identities, citation bounds and agreement with document judgments. On the T9 acquisition, it verified 1,435 PDFs, 118,524 physical pages and 90 original questions. There are 823 distinct question–page pairs and 816 tenant/filter query variants with at least one eligible citation. A human visual audit of page conventions remains separate from these structural checks.

The matched arm uses publisher text with zero-based bounding box page IDs mapped to one-based physical page IDs. The raw arm uses a separate output directory and `--object-manifest refs.json` in addition to the same options. Each PDF basename maps to an immutable object URL and optional strong ETag. The reader verifies the downloaded bytes against the pinned corpus digest. Use a different URL manifest when a signed URL expires. The fixture server is a loopback-only source of authored objects. Do not publish signed URLs. For each arm the manifest records actual document, page-route and chunk counts. For elements spanning several pages, the matched frontend copies the element text to each referenced page. Pages without mapped text contribute no searchable chunks.

## Run one comparison

Use a fresh isolated deployment. Vespa deployments replace the entire application package. The repository's qualification controller provisions disposable instances. With a separate campaign deployment:

```sh
python3 compare.py postgres --endpoint "$PG_DSN" --corpus "$CORPUS" \
  --workload "$WORKLOAD" --out "$RESULT" --qps 2 --k 20 --depth 100
python3 report.py "$RESULT/result.json" --against "$PEER/result.json" --figure migration.pdf
```

On a host with the official macOS binary, `native_release.py --binary /path/to/antfly --state /path/to/new-db-directory --port 37982 --out "$RESULT" --corpus "$CORPUS" --workload "$WORKLOAD" --migration-models "$MODEL_URL"` runs the release on loopback with a fresh credential and shuts it down at completion. Keep the database on a filesystem supported for database durability. The output records platform-specific measurements separately from the Linux Docker cohort.

The corresponding engine names are `antfly`, `opensearch`, `vespa` and `lance`. Endpoints are a server URL, PostgreSQL writer DSN, or fresh Lance directory. Set `ARCHIVE_ANTFLY_AUTH` to the disposable admin credential, `ARCHIVE_VESPA_CONFIG` to its configuration server URL, and optionally `ARCHIVE_OS_AUTH` for Basic authentication. Select the same candidate depth, page budget, hardware and resource limits for paired trials. Alternate execution order. For a shared-worker inference-inclusive migration, pass `--migration-models "$MODEL_URL"`. That mode re-embeds each chunk with B inside the timed interval, compares its vector direction with the frozen independent preparation, and records model calls and elapsed time. The default measures loading precomputed vectors. Client selection of B and acknowledged row counts have different semantics from Antfly's native alias switch and persisted-vector qualification. Use the public native contracts and vector inspection tools for those claims.

The report pairs original questions and bootstraps original-question clusters. Its figure shows acknowledged B rows and A query p95 in five-second bins. Raw observations include failed requests and scheduled arrivals. Review search quality, availability, completed work and the access enforcement boundary before comparing timing.

## Score answers and graph evidence

Run the identical single-call reader against the two raw-object systems and the oracle-page control. Rank only original questions for this arm. Include the same reader revision and context/output budgets in every run.

```sh
python3 answers.py --data "$DATA" --corpus "$RAW_CORPUS" \
  --rankings "$RESULT/a-original-hybrid.json" --model "$READER" \
  --endpoint "$CHAT_COMPLETIONS_URL" --out "$ANSWERS"
python3 answers.py --data "$DATA" --corpus "$RAW_CORPUS" --oracle-pages \
  --model "$READER" --endpoint "$CHAT_COMPLETIONS_URL" --out "$ORACLE"
python3 annotate.py freeze "$DATA/dataset-manifest.json" > sample.json
python3 annotate.py score sample.json adjudicated.json extracted.json
```

The reader receives only the question and selected page text. Gold answers remain with the digest-verified upstream scorer at zero numeric tolerance. Malformed outputs count as misses. Responses retain citations, supplied pages and token usage. Monetary cost requires the applicable dated rate card. The graph sampling command freezes physical pages independently of the questions and extraction output. Annotators record page-supported triples of subject, relation and object, including empty lists. Have independent readers adjudicate disputed identities and citations before evaluating the extractor. The authored graph fixture has separate frozen judgments. Full-corpus results and human annotations require execution and are absent from the fixture receipts.

## Evidence

[Observed v0.3.1 backfill trials](observations/backfill-v031.json) contain all six paired trial phase summaries, original result digests and exclusions. [Five-engine fixture quality](observations/matched-fixture.json) contains the scoped per-query results. Raw graph receipts are provided for [Antfly](observations/antfly-raw-final.json) and [PostgreSQL](observations/postgres-raw-final.json). The source used for the first T9 corpus preparation is frozen at commit `b1b372c`.

To measure extraction quality, run `extract.py --sample sample.json --corpus "$RAW_CORPUS" --model "$READER" --endpoint "$CHAT_COMPLETIONS_URL" --out "$EXTRACTION"`, then pass its assertions.json to the annotation scorer. Annotate using the same paid-for and fiscal-year relations, with a page citation for each triple. Keep annotators blind to the predictions. For the equal-page-budget retrieval ablation, run `expand.py --workload "$WORKLOAD" --rankings "$RESULT/a-after-schema-change-hybrid.json" --assertions "$EXTRACTION/assertions.json" --k 20 --out "$EXPANSION"`. This is a common application-side quality ablation over extracted facts. Its runtime excludes native graph traversal. Missing sampled evidence remains a miss. The native graph comparison is the separate two-hop experiment above.

For an existing versioned S3 bucket, install boto3 and run `s3_sources.py --inventory "$DATA/dataset-manifest.json" --bucket "$BUCKET" --prefix "$PREFIX" --out refs.json`. The resolver reads object metadata. It records each key, version ID and ETag without credentials. Pass this JSON as the corpus object manifest. Each GET names the immutable version, supplies If-Match, checks the returned version and ETag and verifies the original corpus SHA-256. Use `--endpoint` on the resolver and `--s3-endpoint` on the reader for S3-compatible deployments. The published fixture receipts used versioned HTTP on loopback.

To provision the same disposable Docker subjects for the complete prepared corpus, pass `--corpus "$CORPUS" --workload "$WORKLOAD"` to qualify.py and use a fresh output directory. It mounts only the prepared inputs, runs the engines in the requested order, and uses 100 candidates and 20 distinct returned pages. Corpus quality is reported as measured. Use `--engines` to alternate the subject order for paired trials.

Run `audit_pages.py --data "$DATA" --out "$PRIVATE_AUDIT"` to render a deterministic sample of cited physical pages and the matching publisher text side by side as separate local files. The audit form records page alignment and evidence presence independently. Keep these gated audit packets outside the public repository.

## Database-owned migration and durable outbox

Add `--managed-weights "$MODEL_DIRECTORY"` to the Docker controller to also exercise native Antfly indexing and PostgreSQL's transactional outbox on identical source chunks. This arm builds the generation inside each system. The gateway admits the empty-table configuration probe and holds actual B model work. An A reader retrieves a sentinel during the hold. A source write corrects that sentinel and revokes the old tenant, then the gateway releases the earlier work. The controller records completion counts, availability, scheduled traffic, gate events, and independent B vector probes on the resulting sentinels. The archive fixture uses real 384-dimensional BGE models. PostgreSQL returns the stored tuple to the authorized reader. Antfly's unprotected index is queried with the expected vector and its antipode, then its native alias switches the serving index and query encoder together. The separate protected-document qualification checks persisted vectors through the administrator inspection endpoint. The five-row controlled receipts are [Antfly](observations/antfly-managed.json) and [PostgreSQL](observations/postgres-managed.json). Full-corpus managed runs include two added sentinels and use a deterministic partition into the two lifecycle principals. The tenant-overlay retrieval arm separately measures overlapping permissions.
