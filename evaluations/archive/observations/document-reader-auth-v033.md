# Antfly query latency with tenant API keys

Antfly v0.3.3 served dense queries using a separate API key for each tenant's read-only account. The query used an unrestricted predicate, and the database enforced the tenant row filter. Source writes used `sync_level=write`. Each trial used a fresh database on an Apple M4 Max host with an ARM64 Linux Docker VM (14 CPUs, 23.4 GiB).

Each trial copied eight authored pages to 1,024 records. A client scheduled two dense-vector queries per second for 60 seconds against index A. It repeated the schedule after submitting index B for creation. B could become ready before the second window ended. Both rounds used a real query embedder on the same capped service that produced the backfill embeddings. The database had three CPUs and 6 GiB of memory, and the model service had two CPUs and 8 GiB.

End-to-end latency runs from scheduled arrival through embedding, the database request and response parsing. Database-request latency starts after the query vector is ready and ends when the client parses the response. Each p95 uses linear interpolation at the 95th percentile within one 120-arrival window.

| Recorded attempt | Serving DB p95 (ms) | Serving end-to-end p95 (ms) | Backfill-window DB p95 (ms) | Backfill-window end-to-end p95 (ms) |
|---:|---:|---:|---:|---:|
| 1 | 27.1 | 119.1 | 89.1 | 386.4 |
| 5 | 23.7 | 105.8 | 39.3 | 315.5 |

All 480 scheduled queries returned a relevant first result. There were zero unavailable responses, errors, unauthorized hits or email-shaped outputs. Both completed trials passed all 18 checks and produced 1,024 B vectors. A third API-key attempt (4) stopped before either timing window when administrative index creation returned retryable HTTP 409 `table_mutation_conflict`. Attempt 5 repeated its full lifecycle.

The [per-arrival CSV](document-reader-auth-v033.csv) and [trial receipts](document-reader-auth-v033.json) include every API-key attempt from the [original run sequence](https://github.com/antflydb/antfly-paper/blob/fbbbafda317aad9e2d0a3fba7e4f02a34b1c3339/evaluations/archive/observations/document-reader-auth-v033.json).

## Reproduction

Materialize the fixture and model files as described in the [document workload instructions](../../head-to-head/README.md#run). The measurements used the instrument frozen at commit `cbd7547ef386856d8660eaa7ac195e62a66d356c`. From the repository root, set `MODEL_ROOT` to the model directory, make the locked ARM64 client available locally, and run two API-key trials with separate output directories.

```sh
results=evaluations/archive/observations/document-reader-auth-v033.json
mkdir -p results/document-reader-auth
jq '.subject | del(.manifest_sha256)' "$results" > results/document-reader-auth/subject.json
export PAPER_SUBJECT_MANIFEST="$PWD/results/document-reader-auth/subject.json"
export PAPER_PROTOCOL=paper-main-2d456b7cecc0-release-v033
export PAPER_CLIENT_IMAGE="$(jq -r .client_image "$results")"
for i in 1 2
do
  python3 evaluations/head-to-head/scripts/run.py \
    --engine antfly --antfly-reader-auth api_key --initial-sync write \
    --data .data/documents/qualification --models "$MODEL_ROOT" \
    --migration-models .data/document-models --performance --copies 128 \
    --duration 60 --rate 2 --out "results/document-reader-auth/$i-api_key"
done
```
