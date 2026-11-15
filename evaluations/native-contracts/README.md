# Pinned-main qualification

These are candidate qualification gates, separate from the published v0.3.0
cohorts. Set `PAPER_PROTOCOL=paper-main-<commit-prefix>-contracts`,
`PAPER_SUBJECT_MANIFEST=/absolute/path/to/subject.json` and
`PAPER_CLIENT_IMAGE` to the locked ARM64 client image.

The subject manifest contains exactly `name` (`main-` plus the first twelve
commit digits), full `commit`, immutable `image`, `binary_sha256`, and
`build_profile`. Before services start, the harness verifies the image's source
revision label and the actual executable checksum. Retain the build recipe,
compiler hash, submodule pins, runtime base digest and all invocation flags beside
the manifest. A candidate is not a released v0.3.0 observation.

Run each gate into a new directory:

```sh
python3 evaluations/native-contracts/scripts/run.py --out results/main-contracts-01
python3 evaluations/native-contracts/scripts/fixes.py --check invalid-model \
  --models /path/to/historical-model-cache --out results/main-invalid-model-01
python3 evaluations/native-contracts/scripts/fixes.py --check descriptors \
  --rows 100000 --out results/main-descriptors-01
```

The contract gate executes incompatible two-dimensional model spaces, a query
held across an atomic alias replacement, stale compare-and-swap rejection,
idempotent receipt replay, and durable alias recovery. An intentionally wrong
query vector must return the wrong neighbor, establishing that the fixture can
detect an encoder/index mismatch. It separately tests direct-producer source
visibility, multi-table failure atomicity, and combined global, namespace,
table and inherited-role row filters with visible positive controls.

The descriptor gate ingests 100,000 normalized 768-dimensional vectors with
`full_index` acknowledgements at soft nofile 1024, and checks a subsequent
query. It records descriptor samples; this is a regression control rather than
a throughput comparison. The invalid-model gate mounts the original Fastino
cache read-only and requires actionable HTTP 400 `INVALID_MODEL` responses for
both legacy and explicit-v2 extraction requests, with discovery still available.
Neither test establishes real-model accuracy. The original native-privacy
activation issue's inline reproduction and the real document application are
additional gates.

Successful owned resources are reclaimed; failures retain stopped state.
These standalone-process tests do not establish embedded C ABI, distributed
Raft or serverless guarantees. Results remain not run until receipts exist.
