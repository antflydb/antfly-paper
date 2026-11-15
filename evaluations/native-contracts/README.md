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

`campaign.py` runs these main contracts, the 100k descriptor control, the extended
controls, all five core lifecycle adapters, and fresh document qualification on
Antfly and the PostgreSQL outbox. It verifies each result's receipt and source
stability flags. Required main/application failures block timing; peer core
failures are retained independently. If qualification succeeds, it runs three
fresh application trials per architecture in alternating order. The invalid
Fastino-cache diagnostic and issue #1682's inline reproduction run separately.
Use `--qualification-only` to stop before timing; otherwise the full sequence is
explicitly enabled. Pass `--data`, `--models`, `--migration-models` and a new
`--out`. Logs and a progressively written `campaign.json` retain every command,
exit status, elapsed time, instrument commit and subject-manifest hash.

## Candidate build

The initial candidate `18eefbbc53bb29341e9d25f1293e9ff8f9c2ca7a` fails the
release-profile build: its compiled metadata adapter lacks `hasIndexAliases`
and `validateTableIndexMutation`. [Antfly PR #1703](https://github.com/antflydb/antfly/pull/1703)
adds the missing owner projections. The repair candidate is
`4882823a16312d7b7ee84981ef183ebe2489f811`; it is main-derived and not yet a
merged main or released observation. Its GNU ARM64 release-profile build and
Linux image startup pass; the full application remains blocked by the upstream
issues linked in [the application status](../head-to-head/README.md). Its version flag is
`-Dantfly-version=0.3.0-main.4882823a16`. Retain this source distinction in
all result manifests.

To reproduce the original build, initialize recursive
submodules at the pinned source and use the repository's Zig 0.17.0 toolchain.
The server-only build uses the release matrix's Linux ARM64 GNU settings:

```sh
ZIG_GLOBAL_CACHE_DIR="$PWD/.zig-cache/global" ANTFLY_ZIG_MAX_RSS=25769803776 \
  python3 zig/tools/run_bounded_zig_build.py --zig /path/to/zig -- \
  build --build-file zig/build.zig antfly \
  -Dtarget=aarch64-linux-gnu.2.28 -Doptimize=fast -Dstrip=true -Dcpu=baseline \
  -Dantfly-version=0.3.0-main.18eefbbc53bb \
  -Dmetal=false -Dcuda=true -Dpjrt=true -Dsystem-blas=false \
  -Dapple-providers=false -Dantfly-bin-name=antfly \
  --prefix "$PWD/zig/zig-out" --cache-dir "$PWD/zig/.zig-cache" -j1
```

Package `zig/zig-out/bin/antfly` as `antfly` and the installed `share/` tree as
`share/` in a new Docker build context. Build with `Dockerfile.main-overlay`
and `--build-arg ANTFLY_COMMIT=<full-commit>` plus
`--build-arg ANTFLY_VERSION=<binary-version> --platform linux/arm64`.
The build profile is `linux-arm64-gnu-release-overlay-v030`: it preserves the
released runtime libraries and replaces the executable, assets and notices.
It is an experimental CPU-only subject, not a newly released image or evidence
about GPU behavior. Record the resulting immutable image ID and executable
checksum; transfer that same image to comparison hosts rather than rebuilding.
