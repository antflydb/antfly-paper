# Running the retrieval evaluation

Requirements: Docker with native Linux ARM64 support, Python 3.9+ on the
controller, and sufficient disk/RAM for the stated protocol. The dependency
image contains Python 3.12 and hash-locked public packages. No hosted model
calls or provider credentials are used by this precomputed-vector workload.

From the repository root:

```sh
docker build --platform linux/arm64 -t antfly-paper-eval:0.3.0 evaluations/retrieval
python3 evaluations/retrieval/scripts/fetch_subject.py
mkdir -p .data/cohere .data/prepared
curl -fL https://assets.zilliz.com/benchmark/cohere_medium_1m/shuffle_train.parquet -o .data/cohere/train.parquet
curl -fL https://assets.zilliz.com/benchmark/cohere_medium_1m/test.parquet -o .data/cohere/test.parquet
docker run --rm --network none --cpus 4 --memory 6g \
  --mount type=bind,src="$PWD/.data",dst=/data \
  antfly-paper-eval:0.3.0 /app/scripts/prepare_vectors.py \
  --train /data/cohere/train.parquet --test /data/cohere/test.parquet \
  --out /data/prepared/1m
```

For a wiring dataset, repeat preparation with `--size 10000` and a new
`--out /data/prepared/10k`. Never overwrite a prepared manifest or result.
Preparation computes and records exact ground truth separately from engines.

```sh
docker run --rm --network none antfly-paper-eval:0.3.0 \
  -m unittest discover -s /app/scripts -p test_retrieval.py
python3 evaluations/retrieval/scripts/run_retrieval.py \
  --data .data/prepared/10k --out results/wiring-01 --wiring
python3 evaluations/retrieval/scripts/run_retrieval.py \
  --data .data/prepared/1m --out results/retrieval-01 --lifecycles 3
python3 evaluations/retrieval/analysis/status.py results/retrieval-01
```

The image tag is resolved to an immutable local image ID before use and that
ID is recorded. `PAPER_CLIENT_IMAGE` can name another prebuilt dependency
image with the same lockfile. `PAPER_WORKSPACE` optionally places binaries
and working data in a separate runner workspace. `PAPER_SUBJECT_TAG` and
`PAPER_ANTFLY_PROFILE` default to `v0.3.0` and `default`; `tuned` is a separate
explicit experiment. `PAPER_EXACT_CONTROL_QUERIES` defaults to five and must
be recorded if changed. `DOCKER_HOST` selects an isolated engine without
altering the operator's default context.

Every lifecycle writes result/provenance checksums. Progress includes request
ordinals, completed/total, current phase and errors. Checkpoints preserve
provisional aggregates; only final results qualify complete lifecycles.
The full controller writes terminal status and a checksum-verified summary.
Record host CPU, VM allocation, storage type and unrelated background activity
with published evidence. Do not make footprint or throughput claims from
these query-latency measurements.
