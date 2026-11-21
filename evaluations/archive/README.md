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
| Antfly v0.3.1 | Flexible documents, typed metadata, nullable physical sort on retention years, full text and external vectors at default search effort | Separate authenticated readers with native row filters |
| PostgreSQL + pgvector | JSONB metadata with expression indexes, generated tsvector with GIN, HNSW with iterative scan | Distinct non-owner login roles under RLS |
| OpenSearch | Explicit typed mappings, BM25, filtered Lucene HNSW | Trusted adapter supplies the filter |
| Vespa | Typed application schemas, BM25 and nearest-neighbor rank profiles | Trusted adapter supplies the filter |
| LanceDB Embedded | Arrow schema evolution, native FTS, cosine vector search, IVF_FLAT for larger tables | Trusted SDK application supplies the prefilter |

Antfly promotes the optional integer `retention_years` when the schema evolves. The evolved schema declares numeric doc values with `missing_null_policy: nulls_ordered` and physically sorts by `(retention_years, _id)`. Rows without retention keep the field absent. A waits for schema publication before updating existing rows. B starts with the evolved schema. The matched query still uses the authenticated tenant policy, an inclusive numeric range, and the configured candidate depth. The Antfly qualification writes 640 rows with absent fields and cross-tenant distractors, and verifies the complete 16 eligible row IDs, empty matches, and native candidate counts in both generations. Elapsed times are reported separately from these assertions.

A diagnostic on the restored 616,832-row OfficeQA A index compared the original schema, declared numeric doc values, and the evolved nullable sort. The 24 fixed query variants cover questions 85–88, selected after examining slow requests. For question 86 with the archive reader and six-year retention, the physical sort reduced the lexical ranking window from 1,600 to 100 and ranking passes from five to one. Across three 2-QPS replays of those variants without migration, missed arrivals fell from 12/72 to 0/72. Exact filtered vector recall@100 rose from 79.6% to 98.8%. Hybrid judged-page recall@20 fell from 21.7% to 18.6% on this selected subset. These diagnostics use stored row IDs and an exhaustive cosine oracle. The full migration campaign measures service during the B build separately. [Aggregate measurements](observations/officeqa-retention-profile.json)

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

The model endpoint serves the BGE A and B models specified in the document application. Point the weights option at the same mounted files. A completed per-document checkpoint includes its output SHA-256. Use the same command with `--resume` to continue an interrupted preparation. Readers require the final manifest. The compiler checks the original dataset revision, file inventory and checksums, question identities, citation bounds and agreement with document judgments. The [T9 verification receipt](observations/corpus-verification.json) covers 1,435 PDFs, 118,524 physical pages and 90 original questions. There are 823 distinct question–page pairs and 816 tenant/filter query variants with at least one eligible citation. A human visual audit of page conventions remains separate from these structural checks.

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

The bulk comparison measures B separately at the last acknowledged write, complete indexing and a successful two-channel tenant query, and the first verified query after the controller switches table and encoder. Antfly uses `sync_level=write` on B and requires both full-text and vector index counts to match the expected rows. Each other adapter finalizes its indexes and checks its searchable row count. The first after-schema-change query must return eligible hits on both channels on every subject. Cutover includes that first read on the selected table. The A-only baseline and migration use the same 2 QPS schedule with eight concurrent client slots. Receipts distinguish dispatch time, lexical and dense durations, completion, and client overload. On macOS set the standalone server's soft file limit to 65536 before starting the campaign.

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

The [OfficeQA archive report](REPORT.md) gives the completed full-corpus native trials, paired retrieval intervals, migration tails and coverage of the controlled fixtures.

[Observed v0.3.1 backfill trials](observations/backfill-v031.json) contain all six paired trial phase summaries, original result digests and exclusions. [Five-engine fixture quality](observations/matched-fixture.json) contains the scoped per-query results. Raw graph receipts are provided for [Antfly](observations/antfly-raw-final.json) and [PostgreSQL](observations/postgres-raw-final.json). The source used for the first T9 corpus preparation is frozen at commit `b1b372c`.

To measure extraction quality, run `extract.py --sample sample.json --corpus "$RAW_CORPUS" --model "$READER" --endpoint "$CHAT_COMPLETIONS_URL" --out "$EXTRACTION"`, then pass its assertions.json to the annotation scorer. Annotate using the same paid-for and fiscal-year relations, with a page citation for each triple. Keep annotators blind to the predictions. For the equal-page-budget retrieval ablation, run `expand.py --workload "$WORKLOAD" --rankings "$RESULT/a-after-schema-change-hybrid.json" --assertions "$EXTRACTION/assertions.json" --k 20 --out "$EXPANSION"`. This is a common application-side quality ablation over extracted facts. Its runtime excludes native graph traversal. Missing sampled evidence remains a miss. The native graph comparison is the separate two-hop experiment above.

For an existing versioned S3 bucket, install boto3 and run `s3_sources.py --inventory "$DATA/dataset-manifest.json" --bucket "$BUCKET" --prefix "$PREFIX" --out refs.json`. The resolver reads object metadata. It records each key, version ID and ETag without credentials. Pass this JSON as the corpus object manifest. Each GET names the immutable version, supplies If-Match, checks the returned version and ETag and verifies the original corpus SHA-256. Use `--endpoint` on the resolver and `--s3-endpoint` on the reader for S3-compatible deployments. The published fixture receipts used versioned HTTP on loopback.

To provision the same disposable Docker subjects for the complete prepared corpus, pass `--corpus "$CORPUS" --workload "$WORKLOAD"` to qualify.py and use a fresh output directory. It mounts only the prepared inputs, runs the engines in the requested order, and uses 100 candidates and 20 distinct returned pages. Corpus quality is reported as measured. Use `--engines` to alternate the subject order for paired trials.

Run `audit_pages.py --data "$DATA" --out "$PRIVATE_AUDIT"` to render a deterministic sample of cited physical pages and the matching publisher text side by side as separate local files. The audit form records page alignment and evidence presence independently. Keep these gated audit packets outside the public repository.

## Database-owned migration and durable outbox

Add `--managed-weights "$MODEL_DIRECTORY"` to the Docker controller to also exercise native Antfly indexing and PostgreSQL's transactional outbox on identical source chunks. This arm builds the generation inside each system. The gateway admits the empty-table configuration probe and holds actual B model work. An A reader retrieves a sentinel during the hold. A source write corrects that sentinel and revokes the old tenant, then the gateway releases the earlier work. The controller records completion counts, availability, scheduled traffic, gate events, and independent B vector probes on the resulting sentinels. The archive fixture uses real 384-dimensional BGE models. PostgreSQL returns the stored tuple to the authorized reader. Antfly's unprotected index is queried with the expected vector and its antipode, then its native alias switches the serving index and query encoder together. The separate protected-document qualification checks persisted vectors through the administrator inspection endpoint. The five-row controlled receipts are [Antfly](observations/antfly-managed.json) and [PostgreSQL](observations/postgres-managed.json). Full-corpus managed runs include two added sentinels and use a deterministic partition into the two lifecycle principals. The tenant-overlay retrieval arm separately measures overlapping permissions.

For a private, loopback HTTP source backed by the verified acquisition, run
`python3 local_objects.py --data "$DATA" --manifest "$REFS"` and pass the
resulting manifest to the raw corpus builder. This mode addresses PDF bytes by
SHA-256, supplies a strong ETag and verifies conditional reads. It measures
local HTTP acquisition. The versioned S3 protocol is available separately.

For the raw answer arm, pass `--original-only` to `compare.py` or
`native_release.py` to build generation A, check row coverage and retrieve
original-question rankings. Use the same reader endpoint, model artifact and
budgets for every set of rankings and the oracle. Increase
`--request-timeout` on `answers.py` and `extract.py` for a local reader with
long prompts. To prepare the blind human review, run
`python3 review_packet.py --data "$DATA" --sample sample.json --out "$PRIVATE_PACKET"`.
The packet includes the sampled page images, their identities, instructions and
two independent blank forms. An unread page is null. Reviewers explicitly
enter an empty list for a reviewed page with zero supported facts.

Generate a Markdown comparison from completed inference-inclusive receipts with
`python3 campaign_report.py --cohort "$PLATFORM_AND_DEPLOYMENT" first/result.json second/result.json`.
The inputs must match on corpus, retrieval budget, offered load and migration
mode. The report includes aggregate quality, request failures, missing arrivals
and each full receipt's SHA-256. The native figure can be regenerated from the
public minute bins with `migration_figure.py` and Matplotlib.

The Wolfspider raw-reader configuration uses Google's Gemma 4 E4B instruction
QAT q4_0 GGUF (`gemma-4-E4B_q4_0-it.gguf`, SHA-256
`676c35070db6dbe52f93e9c864ee0fba4eddea94b9c875d9cb10daff453fbaee`)
from revision `4b4a2c1d584be7264f87aac328a1bc739ce81b6c`. The local
llama.cpp server (build 12359, commit `b1b770d90`) uses a 32,768-token
context, one slot, and reasoning disabled. The request temperature is zero.
Answer runs use 48,000 evidence characters and 512 output tokens, extraction
uses 24,000 characters and 1,024 output tokens. The shared Florence OCR
weights have SHA-256 `523000d1759e8a61469aaf33378c6c8affe2ff107b6bbfd1b82a44b53f4bd156`.

## Fairness and replacement-encoder controls

See the [comparability audit](COMPARABILITY.md) for five-engine quality, policy boundaries, query timing and the Linux credential controls. Reproduce the public 4,096-row fixture with `python3 fairness_control.py --out controls.json`. It launches isolated Docker containers using pinned images and Python's standard library.

To repeat the database part on the prepared corpus, use `python3 corpus_controls.py --corpus "$CORPUS" --workload "$WORKLOAD" --out "$PRIVATE_RESULTS" --probe 284`. Docker needs read access to the paper tree and inputs and write access to the output directory. Each of the five configurations loads frozen B vectors and records write-call wall time inside the total build time. Two rounds reverse the arm order. This measures database, adapter and serving costs independently of B inference. The original shared-inference PostgreSQL trial can be repeated separately with `compare.py postgres --readiness-probe-ordinal 284 --postgres-lexical-control` plus the normal corpus, workload, endpoint, model service and output arguments. Its supplemental A lexical and hybrid replays use a disjunction of the distinct English lexemes on the same RLS-protected index after timing. Primary rankings retain the original parser.

For the separate cross-family experiment, prepare a fresh corpus with `corpus.py --replacement google/embeddinggemma-2`. Mount the same safetensors weights at the client and model service. The builder sends document and query task types to the release runtime, and the timed worker reads the replacement model from the corpus manifest. The authored smoke can be reproduced with `miniature.py "$FIXTURE" --models "$MODEL_URL"` followed by `native_release.py` with the fixture as both corpus and workload and the same migration model service.

### Native document ingestion and LightOnOCR

The five-engine OfficeQA migration uses a single Python chunker in corpus preparation. Within each physical page it splits on whitespace, takes consecutive groups of 160 words with zero overlap, keeps the short tail and assigns a stable page and chunk ID. Empty pages produce no chunks. Each engine indexes those bodies and vectors. Tenant copies reuse the prepared chunks. The controlled managed-backfill experiment also starts with these chunks. This makes page retrieval comparable across engines.

For the separate native ingestion measurement, use released Antfly standalone with the installed revision of LightOnOCR-3-0.8B and supply source URLs. The two [enrichments](lighton-enrichments.json) select native extraction with automatic OCR fallback and native stored, full-text-indexed chunks targeting 160 tokens with zero overlap. The application submits a source record. The database owns fetching, page selection, OCR and chunk publication. The object store still serves the bytes. The matched application arm uses PDFium, the same reader via the inference API and the page-local 160-word splitter. Word and native-token boundaries can differ.

With a dedicated standalone, the configured object host allowed, and a versioned URL manifest produced by the archive harness, run:

```sh
python3 native_documents.py --endpoint "$ANTFLY_URL" --table paperdocuments \
  --refs officeqa-refs.json --out private-native-receipts
```

The [three-source release check](observations/lighton-ingestion-fixture.json) includes a scan, a born-digital page and a long born-digital page. The scan selected OCR, the long page produced two native chunks, and all three source documents were searchable. The native process ran with a 6,144 MiB inference scratch budget; the default 2,048 MiB budget rejected this scan. The search check looked up one expected term per source in the full-text index. The short PDFs are the first-version ledger and appendix in `fixture_archive.py`. The long PDF was generated with `fixture.native_pdf` from 19 repetitions of the sentence “The keeper inspected each brass lantern and entered measurements into the ship ledger.”

For the application corpus, pass `--ocr-model lightonai/LightOnOCR-3-0.8B:safetensors@36d8636925327c671bd8e479c517a004f7566bd9` to `corpus.py` with a fresh output directory. Per-document checkpoints retain measured prompt and completion tokens with reporting coverage, declared unmeasured allowances, submitted pixels, OCR calls and wall time. The native receipt records attempted, selected, retained and failed OCR pages plus emitted units and chunks. The v0.3.1 native manifest does not expose equivalent token counts. Compare time from source submission through publication separately from time inside individual model calls. The fixture's timings include polling and warm-up effects.

For implementation effort, compare the application-owned steps, the running processes, and the checked-in configuration for the same requirements. Exclude dataset acquisition, assertions and measurement code on both sides. The matched application frontend here is one reference implementation shared across engines. Its size does not establish a minimum for PostgreSQL, OpenSearch, Vespa or LanceDB.

### PDF page-image to passage-index qualification

The public fixture uses the same EmbeddingGemma 2 model identity and 768-dimensional query vector for both generations. Three single-page PDFs include a scan and a page long enough for two chunks. During B backfill the client corrects the scan and moves a second source outside a reader's tenant. An API-key reader searches the live alias until the compare-and-swap completes. A fourth, two-page PDF checks page attribution after cutover. A separate control renders PNGs in the application and lets Antfly embed the images. This control uses one physical page per source row.

Use a fresh local v0.3.2 standalone on the same machine as the fixture, with both model bundles installed in its models directory. The run creates two disposable tables and two readers. The native leg fetches PDF URLs directly. The control uses PDFium at 144 DPI. The fixture dependencies are `pypdfium2==4.30.0` and `pillow==13.0.0`.

```sh
export ANTFLY_BOOTSTRAP_ADMIN_PASSWORD="$(openssl rand -hex 24)"
export ARCHIVE_ANTFLY_AUTH="admin:$ANTFLY_BOOTSTRAP_ADMIN_PASSWORD"
export ANTFLY_MODELS_DIR="${ANTFLY_MODELS_DIR:-$HOME/.antfly/inference/models}"
antfly inference pull hf:google/embeddinggemma-2:bf16-safetensors-bundle-v1 --models-dir "$ANTFLY_MODELS_DIR"
antfly inference pull hf:lightonai/LightOnOCR-3-0.8B:safetensors@36d8636925327c671bd8e479c517a004f7566bd9 --models-dir "$ANTFLY_MODELS_DIR"
state="$(mktemp -d)"
cat > "$state/config.json" <<'JSON'
{"public_inference":{"enabled":true},"remote_content":{"security":{"allowed_hosts":["127.0.0.1"],"block_private_ips":false}}}
JSON
antfly standalone --host 127.0.0.1 --port 38725 --auth true --health false \
  --config "$state/config.json" --models-dir "$ANTFLY_MODELS_DIR" \
  --data-dir "$state/db" --inference-scratch-budget-mb 6144 > "$state/server.log" 2>&1 &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true' EXIT
curl --retry 30 --retry-connrefused --retry-delay 1 -fsS -u "$ARCHIVE_ANTFLY_AUTH" \
  http://127.0.0.1:38725/db/v1/tables > /dev/null
uv run --with pypdfium2==4.30.0 --with pillow==13.0.0 python pdf_vector_migration.py \
  --endpoint http://127.0.0.1:38725 --out "$state/qualification"
```

For corpus runs through `native_release.py`, use `--expected-version 0.3.2` to pin the subject. The runner writes the observed version and binary SHA-256 to `subject.json` in its state directory. The historical v0.3.1 cohorts can be replayed with `--expected-version 0.3.1`.

The client writes per-leg JSON receipts. A failed behavioral check gives a nonzero exit even if the database reports complete vector coverage. Fixture timings include model warm-up effects and use a single closed-loop reader. Use the archive campaign for matched latency comparisons.

For the full representation comparison, the admission condition is that a tenant-scoped query on each serving generation returns the expected physical page. Score dense retrieval first on the same 90 questions, filters, and 20-distinct-page budget, then score hybrid retrieval on B. Record rendered pages, OCR calls and reported tokens, chunks, vectors, storage, backfill time, query latency and cutover separately. Include application rendering, OCR and indexing costs for each engine. Keep the publisher-text encoder replacement as a separate experiment.

#### Matched representation fixture for all five engines

Run the shared-input arm to compare Antfly, PostgreSQL/pgvector, OpenSearch, Vespa and LanceDB. A common worker renders the initial pages at 144 DPI, reads embedded text with PDFium, sends pages with less than 50 non-whitespace characters to LightOnOCR, and forms page-local chunks of up to 160 words. It embeds the images, chunks and questions with the same Gemma model identity and writes one input file for all adapters. Use a loopback inference endpoint serving both models, then run from the repository root:

```sh
python evaluations/archive/pdf_vector_compare.py prepare --models "$MODEL_URL" --out "$INPUTS"
python evaluations/archive/qualify.py --pdf-inputs "$INPUTS/inputs.json" --out "$RESULTS"
```

Every adapter serves A while B is partially populated. The client applies the same ledger correction and tenant change to both generations, waits for both public and audit readers to reflect the change, then routes new reads to B. The gates check dense page retrieval on A and B, hybrid retrieval on B, replacement of the old lexical evidence and the physical page of the additional two-page PDF. Each receipt includes the shared-input SHA-256, access mechanism, individual checks, and actual probe count. The fixture measures retrieval and application-orchestrated migration over matched representations. The separate Antfly-native run measures sourcing those representations directly from PDF URLs.

#### Full OfficeQA representation campaign

Use the v0.3.2 binary that passed the PDF fixture. Start its loopback inference server with the qualified Gemma and LightOnOCR bundles and `--scratch-budget-mb 16384`. On Wolfspider the first 233 PDFs ran with a 6 GiB scratch budget. A later OCR request required 12.63 GiB. Preparation resumed with a 16 GiB budget, which the full native PDF trial also uses. The preparation and database stages run serially on the benchmark host. Paths for the prepared inputs, raw rankings and database state must point to private storage.

```sh
python evaluations/archive/pdf_archive_inputs.py --models "$MODEL_URL" --data "$OFFICEQA" \
  --workload "$WORKLOAD" --out "$INPUTS" --subject "$(shasum -a 256 "$ANTFLY_BIN" | cut -d' ' -f1)"
python evaluations/archive/pdf_archive_campaign.py --inputs "$INPUTS" --data "$OFFICEQA" \
  --workload "$WORKLOAD" --binary "$ANTFLY_BIN" --models-dir "$MODELS_DIR" \
  --out "$RESULTS" --state "$DB_STATE"
```

Preparation checks the frozen inventory, renders every physical page at 144 DPI, and checkpoints both representations per PDF. Generation A contains one 768-dimensional image vector per page. Generation B contains same-model vectors for 160-word page-local passages. Pages with fewer than 50 non-whitespace characters go to LightOnOCR at 150 DPI. The shared worker requests up to 8,192 output tokens on the first call. A response that truncates at that ceiling retains the available PDF text layer and records the physical page and failed attempt. Pages with no usable text remain in the image index and in the original judgment population. OCR token totals count only calls that returned usage. Rerunning the preparation command verifies completed checkpoints and resumes remaining PDFs. When resuming an older run, the worker freezes the already-committed prefix in `identity.json` as `legacy_ocr_pdfs`. Those PDFs used 4,096 tokens with one retry at 8,192. To reproduce that schedule from scratch, pass `--legacy-ocr-pdfs N` with the recorded prefix length. Each new PDF receipt records its token limits. The final manifest groups completed-PDF processing time, OCR wall time, pages and calls by token policy. Processing time excludes downtime and abandoned attempts.

The five shared-input trials measure database build and serving time using identical prepared vectors. Both generations start with the frozen tenant fields, and Antfly uses `sync_level=write` with an explicit searchable-coverage check. Each trial verifies the distinct expected row counts of A and B, serves dense A queries during B construction, and switches the application route after B coverage and a scoped dense probe. The sixth trial supplies content-addressed PDF URLs directly to Antfly, measures managed indexing and switches the native alias. It uses the release's native text extraction, OCR fallback and 160-token chunker. The application OCR token policy applies only to shared-input preparation. The native trial reports the engine's own OCR outcomes. Tenant-visible source copies and native extraction counts are reported separately. Correction and revocation are exercised on the authored fixture.

The campaign records original-question citation recall@20, tenant-filtered recall, eligible results, build and cutover time, live traffic, and final database directory size. A is scored on dense retrieval. B is scored on dense, lexical and hybrid retrieval. Each channel gets 100 candidates. A failed subject writes an attempt receipt and independent subjects continue. Keep the host-native and VM deployment boundaries with any reported latency. The live query schedules differ between the matched open-loop runs and the native closed-loop alias diagnostic.
