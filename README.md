# Antfly paper and evaluations

Open-source protocols and reproducible evaluation tools for Antfly. Code is
Apache-2.0 licensed. This repository contains the evaluation implementation;
it does not yet publish a completed paper evaluation or comparative results.

## Retrieval evaluation

The primary subject is **Antfly v0.3.0 at its server default search effort
of 0.5**. Requests omit `search_effort`; the default is not selected by tuning.
Peers use calibration-only configuration selection at matched recall and
authorization/completeness requirements.

The matrix groups three families: Antfly, PostgreSQL retrieval, and LanceDB.
PostgreSQL shared/partial pgvector indexes and native ParadeDB vectors are
separately measured configurations, not three independent architecture claims.
Tuning grids and exact-search diagnostics belong in supplementary reporting.

See [the frozen retrieval protocol](evaluations/retrieval/protocol.md) and
[execution instructions](evaluations/retrieval/README.md). The instruments
support native Linux ARM64 containers with separately capped measurement
clients. Source corpora and release binaries are downloaded separately and
verified; data, models, credentials, logs and generated results stay out of Git.

## Scope

Filtered retrieval alone does not establish the inference-native thesis.
Lifecycle/revision recovery, correction/deletion, complete application effort,
speaker-boundary and HBC/LEANN experiments require separate evidence. Closest
ingest-inference prior art such as Vespa or Elasticsearch must be considered
alongside pipeline and embedded-SDK baselines for those claims.

The standalone runner owns its temporary Docker resources. External campaign
runners may pin this repository as a submodule and handle host preparation,
artifact storage and publication handoff separately. Never combine results
from different release tags or protocol versions into one reported comparison.
