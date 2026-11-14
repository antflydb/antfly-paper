# Synthetic lifecycle controls: lifecycle-v1

A bounded executable subset of the paper's lifecycle experiments, not the full
paper evaluation. Subjects and official native ARM64 image digests are pinned
in [subjects.json](subjects.json). The same eight synthetic source documents
and orthogonal equal-dimension A/B embedding spaces are defined in fixture.py.
Each run records fixture, instrument, dependency and subject-manifest hashes.

```sh
# Build the dependency image and pull each selected image in subjects.json.
docker build --platform linux/arm64 -t antfly-paper-eval:0.3.0 evaluations/retrieval
python3 -m unittest discover -s evaluations/lifecycle/scripts -p test_lifecycle.py
python3 evaluations/lifecycle/scripts/run.py --engine antfly --out results/lifecycle-antfly-01
python3 evaluations/lifecycle/scripts/run.py --engine postgres-outbox --out results/lifecycle-postgres-01
python3 evaluations/lifecycle/scripts/run.py --engine lancedb --out results/lifecycle-lancedb-01
python3 evaluations/lifecycle/scripts/run.py --engine vespa --out results/lifecycle-vespa-01
python3 evaluations/lifecycle/scripts/run.py --engine opensearch --out results/lifecycle-opensearch-01
```

Vespa is deliberately explicit, not a default shared-gauntlet entry. Use a
fresh output path for every run. PAPER_CLIENT_IMAGE and DOCKER_HOST select the
dependency image and Docker engine. No paid model or public provider call is
made. See [baseline API research](research.md).

## What actually runs

| Check | Antfly / PostgreSQL outbox | LanceDB / Vespa / OpenSearch |
| --- | --- | --- |
| Derived-ready source ingestion and returned-vector validation | Yes | Yes |
| Metadata-only update without re-embedding the body | Yes | Supported native scalar update path |
| Source update/delete while an old provider reply is held | Async source acknowledgements; superseded work must not land | Not claimed by the synchronous native path |
| Crash/restart with a provider call held | Native server / custom worker respectively | Not claimed |
| Published state survives abrupt restart | Server / worker respectively | SDK process / whole container respectively |
| Separate A0, B1, A2 generations and matching query encoder | Named indexes / artifact generations | Separate tables / synthetic fields / model IDs and indexes |
| Old-generation reads while creating replacement | Yes | Yes |

Vespa runs its built-in OpenAI-compatible embedder, service convergence and
stored-data reindexer. OpenSearch uses native semantic fields and deployed ML
Commons connectors. LanceDB uses native embedding functions and merge_insert;
its private measurement facade is not a required production service. The
PostgreSQL application is a custom durable outbox with version-conditional
publication, not a Temporal, Dagster or pgai implementation. Count that code.

## Meaning and limits

Every checked read must contain the expected live IDs and fixture vectors;
empty, incomplete, obsolete, wrong-space and orphaned responses fail. The
oracle's adversarial tests and a held-request identity test qualify the
instrument separately. Actual queries use the selected generation's query
vector. Cutover is client-coordinated, not a global transactional alias change.

No inference-performance or matched-resource latency ranking follows from
these stub controls. Two shared providers have 0.5 CPU / 256 MiB each; engines
have 4 CPU / 12 GiB, and PostgreSQL's custom worker has an additional 1 CPU /
1 GiB. The standard-library host controller is not a capped measurement client.
A declared nofile=65536:524288 avoids release issue antflydb/antfly#1670; do not
pool these controls with default-limit retrieval failures.

Owned bridge networks expose only random loopback ports to the controller.
Docker internal-only networks do not publish ports on current Desktop; the
bridge permits networking but instruments call only their owned local/provider
addresses. OpenSearch separately allowlists exactly those private model URLs.
Resources and synthetic credentials are attempt-owned. Success reclaims state;
failed/disputed state and bounded redacted logs remain for diagnosis.

Results explicitly list not-run work: concurrent writes or held replies across
migration, crash during migration, exact internal failpoints, wrong/missing
provider-identity acknowledgements, heterogeneous stages, committed
nondeterministic decisions, distributed cutovers, real models, privacy and
policy attacks. Broader draft protocols define that expansion. Editing source
during a run marks the result instrument-changed rather than qualifying it.
