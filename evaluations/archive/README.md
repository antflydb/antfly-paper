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
