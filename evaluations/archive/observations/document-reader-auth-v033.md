# Tenant reader credentials on the 1,024-record document fixture

Four Antfly v0.3.3 latency trials were scheduled sequentially on an Apple M4 Max host with an ARM64 Linux Docker VM (14 CPUs, 23.4 GiB). The first pair used API keys then Basic, and the second pair reversed the order. The fourth attempt stopped during correctness qualification and was repeated against a fresh database as attempt five. Each tenant had a read-only account and the same row filter in both conditions. The query had an unrestricted predicate, so Antfly had to enforce the tenant restriction. Only the credential on tenant requests changed. Administrative setup, source writes and readiness polling used the same bootstrap login. Source writes used `sync_level=write`.

Each trial copied eight authored pages to 1,024 records. A client scheduled two dense-vector queries per second for 60 seconds against index A. It repeated the schedule after submitting index B for creation. B could become ready before the second window ended. Both rounds used a real query embedder on the same capped service that produced the backfill embeddings. The database had three CPUs and 6 GiB of memory, and the model service had two CPUs and 8 GiB.

End-to-end latency runs from scheduled arrival through embedding, the database request and response parsing. Database-request latency starts after the query vector is ready and ends when the client parses the response. Each p95 uses linear interpolation at the 95th percentile within one 120-arrival window.

| Trial | Reader credential | Serving DB p95 (ms) | Serving end-to-end p95 (ms) | Backfill-window DB p95 (ms) | Backfill-window end-to-end p95 (ms) |
|---:|---|---:|---:|---:|---:|
| 1 | API key | 27.1 | 119.1 | 89.1 | 386.4 |
| 2 | Basic | 78.8 | 176.1 | 121.3 | 460.4 |
| 3 | Basic | 74.5 | 161.8 | 84.0 | 407.2 |
| 4 | API key | — | — | — | — |
| 5 | API key | 23.7 | 105.8 | 39.3 | 315.5 |

The serving-only database-request p95 was 51.7 ms lower with keys in the first pair and 50.8 ms lower in the reverse-order pair. End-to-end p95 was about 57 and 56 ms lower. In the backfill window, both pairs also favored API keys, with greater variation across trials.

All 960 scheduled queries returned an authorized, relevant first result. There were zero errors, unavailable responses, unauthorized hits or email-shaped outputs. Each completed trial passed all 18 checks and reported 1,024 produced B vectors. Attempt four passed its first ten checks, then the administrative B-index creation returned retryable HTTP 409 `table_mutation_conflict` during the eight-document qualification. It produced no latency observations. Attempt five repeated the whole lifecycle with the same instrument. The historical [controlled backfill timings](backfill-v031.json) measure time from index creation to observed complete coverage.

The [per-arrival CSV](document-reader-auth-v033.csv) contains timings and correctness outcomes. The [trial receipts](document-reader-auth-v033.json) include counts, precision-preserving quantiles, resource caps, fixture and model fingerprints, and SHA-256 digests for the original measurements.

## Reproduction

Materialize the fixture and model files as described in the [document workload instructions](../../head-to-head/README.md#run). The runner in this revision matches the instrument frozen at commit `cbd7547ef386856d8660eaa7ac195e62a66d356c`. From the repository root, set `MODEL_ROOT` to the model directory, make the locked ARM64 client available locally, and run the four trials with separate output directories.

```sh
results=evaluations/archive/observations/document-reader-auth-v033.json
mkdir -p results/document-reader-auth
jq '.subject | del(.manifest_sha256)' "$results" > results/document-reader-auth/subject.json
export PAPER_SUBJECT_MANIFEST="$PWD/results/document-reader-auth/subject.json"
export PAPER_PROTOCOL=paper-main-2d456b7cecc0-release-v033
export PAPER_CLIENT_IMAGE="$(jq -r .client_image "$results")"
i=0
for auth in api_key basic basic api_key
do
  i=$((i+1))
  python3 evaluations/head-to-head/scripts/run.py \
    --engine antfly --antfly-reader-auth "$auth" --initial-sync write \
    --data .data/documents/qualification --models "$MODEL_ROOT" \
    --migration-models .data/document-models --performance --copies 128 \
    --duration 60 --rate 2 --out "results/document-reader-auth/$i-$auth"
done
```
