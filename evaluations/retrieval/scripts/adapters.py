"""Native campaign adapters. No shell/process startup occurs in query().

Postgres uses eight distinct non-owner login roles with fixed RLS policies.
Antfly uses eight reader accounts with server-enforced row filters. Embedded
LanceDB's filter is supplied by this trusted application boundary, not native RLS.
Only the deliberate starvation ablation filters a global top ten afterwards.
"""

import os
import time

import httpx
import numpy as np
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo


def cell_number(cell):
    if type(cell) is not int or not 0 <= cell < 8:
        raise ValueError("cell must be an authenticated permission-set ordinal")
    return cell


def vector_literal(vector):
    return "[" + ",".join(format(float(x), ".9g") for x in vector) + "]"


def notify(adapter, **fields):
    callback = getattr(adapter, "progress", None)
    if callback:
        callback(**fields)


class Postgres:
    def __init__(self, kind, url, password):
        if kind not in {"pgvector", "pgvector-partial", "paradedb"}:
            raise ValueError(kind)
        self.kind, self.url, self.password = kind, url, password
        self.admin = psycopg.connect(url, autocommit=True)
        self.readers = {}
        self.metadata = {
            "authorization": "native fixed-role RLS",
            "query_boundary": "persistent psycopg cursor to completed rows",
        }

    def load(self, vectors, permissions):
        dim = vectors.shape[1]
        self.admin.execute("CREATE EXTENSION IF NOT EXISTS vector")
        flags = ",".join(f"allow{i} boolean NOT NULL" for i in range(8))
        self.admin.execute(
            f"CREATE TABLE documents (id bigint PRIMARY KEY, embedding vector({dim}) NOT NULL,{flags})"
        )
        with self.admin.cursor().copy("COPY documents FROM STDIN") as copy:
            for identity, vector in enumerate(vectors):
                mask = int(permissions[identity])
                copy.write_row(
                    (
                        identity,
                        vector_literal(vector),
                        *(bool(mask & (1 << i)) for i in range(8)),
                    )
                )
                if (identity + 1) % 16384 == 0 or identity + 1 == len(vectors):
                    notify(
                        self,
                        state="copy-in-progress",
                        rows_sent=identity + 1,
                        total_rows=len(vectors),
                    )
        self.admin.execute("ALTER TABLE documents ENABLE ROW LEVEL SECURITY")
        self.admin.execute("ALTER TABLE documents FORCE ROW LEVEL SECURITY")
        for cell in range(8):
            role = sql.Identifier(f"paper_reader_{cell}")
            self.admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    role, sql.Literal(self.password)
                )
            )
            self.admin.execute(sql.SQL("GRANT SELECT ON documents TO {}").format(role))
            self.admin.execute(
                sql.SQL(
                    "CREATE POLICY {} ON documents FOR SELECT TO {} USING ({})"
                ).format(
                    sql.Identifier(f"acl_{cell}"), role, sql.Identifier(f"allow{cell}")
                )
            )
            connection = psycopg.connect(
                make_conninfo(
                    self.url, user=f"paper_reader_{cell}", password=self.password
                ),
                autocommit=True,
            )
            connection.execute("SET statement_timeout='120s'")
            connection.execute("SET work_mem='64MB'")
            assert connection.execute(
                "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user"
            ).fetchone() == (False,)
            self.readers[cell] = connection
        self.metadata["versions"] = self.admin.execute(
            "SELECT extname,extversion FROM pg_extension WHERE extname IN ('vector','pg_search') ORDER BY extname"
        ).fetchall()
        self.metadata["postgres_version"] = self.admin.execute(
            "SHOW server_version"
        ).fetchone()[0]

    def build(self):
        self.metadata["build_config"] = {
            "maintenance_work_mem": self.admin.execute(
                "SHOW maintenance_work_mem"
            ).fetchone()[0],
            "parallel_maintenance_workers": self.admin.execute(
                "SHOW max_parallel_maintenance_workers"
            ).fetchone()[0],
        }
        notify(self, state="index-build-in-progress", index="retrieval")
        if self.kind == "paradedb":
            columns = ",".join(f"allow{i}" for i in range(8))
            self.admin.execute(
                f"CREATE INDEX retrieval ON documents USING paradedb (id,{columns},embedding vector_cosine_ops)"
            )
            self.metadata["index"] = (
                "ParadeDB native cosine, indexed boolean filters, default quantization"
            )
        elif self.kind == "pgvector-partial":
            for cell in range(8):
                notify(
                    self,
                    state="index-build-in-progress",
                    index=f"retrieval_{cell}",
                    completed_indexes=cell,
                    total_indexes=8,
                )
                self.admin.execute(
                    f"CREATE INDEX retrieval_{cell} ON documents USING hnsw (embedding vector_cosine_ops) WITH (m=16,ef_construction=200) WHERE allow{cell}"
                )
            self.metadata["index"] = (
                "eight partial HNSW indexes; m=16, ef_construction=200"
            )
        else:
            self.admin.execute(
                "CREATE INDEX retrieval ON documents USING hnsw (embedding vector_cosine_ops) WITH (m=16,ef_construction=200)"
            )
            self.metadata["index"] = "one shared HNSW; m=16, ef_construction=200"
        self.admin.execute("ANALYZE documents")

    def candidates(self):
        if self.kind == "paradedb":
            return [
                {"mode": "ann", "probe": p, "scan_levels": levels}
                for levels in (3, 0)
                for p in (0.02, 0.1, 0.5, 1.0)
            ] + [{"mode": "exact"}]
        return [
            {"mode": order, "ef": ef, "scan_limit": limit, "scan_memory": 4}
            for order in ("strict_order", "relaxed_order")
            for limit in (20000, 1000000)
            for ef in (40, 100, 200, 400)
        ] + [{"mode": "exact"}]

    def configure(self, cell, candidate):
        connection = self.readers[cell_number(cell)]
        connection.execute("SET enable_indexscan=on; SET enable_bitmapscan=on")
        if candidate["mode"] == "exact":
            # Exact distance is computed in a materialized authorized set;
            # no ANN ordering is available to the outer sort.
            return
        if self.kind == "paradedb":
            connection.execute(
                "SELECT set_config('paradedb.vector_cluster_max_probe',%s,false)",
                (str(candidate["probe"]),),
            )
            connection.execute(
                "SELECT set_config('paradedb.max_scan_levels',%s,false)",
                (str(candidate["scan_levels"]),),
            )
        else:
            for name, value in (
                ("iterative_scan", candidate["mode"]),
                ("ef_search", candidate["ef"]),
                ("max_scan_tuples", candidate["scan_limit"]),
                ("scan_mem_multiplier", candidate["scan_memory"]),
            ):
                connection.execute(
                    "SELECT set_config(%s,%s,false)", ("hnsw." + name, str(value))
                )

    def statement(self, cell, candidate):
        field = f"allow{cell_number(cell)}"
        if candidate["mode"] == "exact":
            return f"WITH authorized AS MATERIALIZED (SELECT id,embedding FROM documents WHERE {field}) SELECT id FROM authorized ORDER BY embedding <=> %s::vector LIMIT 10"
        prefix = "id @@@ pdb.all() AND " if self.kind == "paradedb" else ""
        query = f"SELECT id,embedding <=> %s::vector AS distance FROM documents WHERE {prefix}{field} ORDER BY distance LIMIT 10"
        if candidate["mode"] == "relaxed_order":
            query = f"WITH candidates AS MATERIALIZED ({query}) SELECT id FROM candidates ORDER BY distance + 0"
        return query

    def query(self, vector, cell, candidate):
        return [
            int(row[0])
            for row in self.readers[cell_number(cell)]
            .execute(self.statement(cell, candidate), (vector_literal(vector),))
            .fetchall()
        ]

    def explain(self, vector, cell, candidate):
        return (
            self.readers[cell]
            .execute(
                "EXPLAIN (FORMAT JSON,COSTS OFF) " + self.statement(cell, candidate),
                (vector_literal(vector),),
            )
            .fetchone()[0]
        )

    def authorized_count(self, cell):
        # No caller predicate: test the database policy itself.
        return (
            self.readers[cell_number(cell)]
            .execute("SELECT count(*) FROM documents")
            .fetchone()[0]
        )

    def global_top10(self, vector):
        # Privileged internal result never leaves this trusted adapter unfiltered.
        return [
            r[0]
            for r in self.admin.execute(
                "WITH all_rows AS MATERIALIZED (SELECT id,embedding FROM documents) SELECT id FROM all_rows ORDER BY embedding <=> %s::vector LIMIT 10",
                (vector_literal(vector),),
            )
        ]

    def close(self):
        for reader in self.readers.values():
            reader.close()
        self.admin.close()


class Antfly:
    def __init__(self, url, password):
        self.url, self.password = url, password
        self.admin = httpx.Client(base_url=url, auth=("admin", password), timeout=120)
        deadline = time.monotonic() + 60
        while True:
            try:
                response = self.admin.get("/db/v1/tables", timeout=2)
                if response.status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if time.monotonic() >= deadline:
                self.admin.close()
                raise TimeoutError("Antfly readiness deadline exceeded")
            time.sleep(0.1)
        self.readers = {}
        self.metadata = {
            "authorization": "native authenticated row filters",
            "query_boundary": "persistent HTTP client through parsed response",
            "index": "external full-vector cosine index",
            "version": os.environ.get("PAPER_SUBJECT_TAG", "v0.3.0"),
            "configuration_profile": os.environ.get("PAPER_ANTFLY_PROFILE", "default"),
        }

    def call(self, client, method, path, body=None):
        response = client.request(method, path, json=body)
        response.raise_for_status()
        return response.json() if response.content else None

    def load(self, vectors, permissions):
        self.call(self.admin, "POST", "/db/v1/tables/documents", {"num_shards": 1})
        self.call(
            self.admin,
            "POST",
            "/db/v1/tables/documents/indexes/vec",
            {
                "type": "embeddings",
                "dimension": vectors.shape[1],
                "distance_metric": "cosine",
                "external": True,
            },
        )
        for offset in range(0, len(vectors), 256):
            documents = {}
            for identity in range(offset, min(offset + 256, len(vectors))):
                mask = int(permissions[identity])
                documents[f"d{identity:09d}"] = {
                    "_embeddings": {"vec": vectors[identity].tolist()},
                    **{
                        f"allow{i}": "yes" if mask & (1 << i) else "no"
                        for i in range(8)
                    },
                }
            response = self.call(
                self.admin,
                "POST",
                "/db/v1/tables/documents/batch",
                {"inserts": documents, "sync_level": "full_index"},
            )
            if response.get("status") != "committed":
                raise RuntimeError(
                    "index acknowledgement incomplete: " + str(response.get("status"))
                )
            notify(
                self,
                state="full-index-committed",
                rows_committed=min(offset + 256, len(vectors)),
                total_rows=len(vectors),
            )
        for cell in range(8):
            user = f"reader{cell}"
            self.call(
                self.admin,
                "POST",
                f"/auth/v1/users/{user}",
                {
                    "password": self.password,
                    "initial_policies": [
                        {
                            "resource": "documents",
                            "resource_type": "table",
                            "type": "read",
                        }
                    ],
                },
            )
            self.call(
                self.admin,
                "PUT",
                f"/auth/v1/users/{user}/row-filters/documents",
                {"term": {f"allow{cell}": "yes"}},
            )
            self.readers[cell] = httpx.Client(
                base_url=self.url, auth=(user, self.password), timeout=120
            )

    def build(self):
        pass  # Antfly's index is maintained during ingestion.

    def candidates(self):
        if os.environ.get("PAPER_ANTFLY_PROFILE", "default") == "default":
            return [{"mode": "default", "effort": 0.5}]
        return [
            {"mode": "ann", "effort": effort} for effort in (0.05, 0.1, 0.25, 0.5, 1.0)
        ]

    def configure(self, cell, candidate):
        cell_number(cell)

    def query_as(self, client, vector, candidate):
        body = {
            "table": "documents",
            "embeddings": {"vec": vector.tolist()},
            "limit": 10,
            "filter_query": {"match_all": {}},
            "fields": [],
        }
        if candidate.get("mode") != "default":
            body["search_effort"] = candidate["effort"]
        response = self.call(
            client,
            "POST",
            "/db/v1/query",
            body,
        )["responses"][0]
        if response.get("status") != 200:
            raise RuntimeError("Antfly query did not succeed")
        return [int(hit["_id"][1:]) for hit in response.get("hits", {}).get("hits", [])]

    def query(self, vector, cell, candidate):
        return self.query_as(self.readers[cell_number(cell)], vector, candidate)

    def global_top10(self, vector):
        return self.query_as(self.admin, vector, {"effort": 1.0})

    def explain(self, vector, cell, candidate):
        return {
            "request": {
                "search_effort": "omitted (server default 0.5)"
                if candidate.get("mode") == "default"
                else candidate["effort"],
                "caller_filter": "match_all",
            },
            "server_plan": None,
            "note": "No execution-plan endpoint used; effort 1 is the exact-search diagnostic.",
        }

    def close(self):
        for reader in self.readers.values():
            reader.close()
        self.admin.close()


class Lance:
    def __init__(self, directory):
        import lancedb

        self.db = lancedb.connect(directory)
        self.metadata = {
            "authorization": "trusted application prefilter; Embedded has no native RLS",
            "query_boundary": "embedded Python query through to_arrow",
            "version": lancedb.__version__,
        }

    def load(self, vectors, permissions):
        import pyarrow as pa

        dim = vectors.shape[1]
        schema = pa.schema(
            [
                ("id", pa.int64()),
                ("vector", pa.list_(pa.float32(), dim)),
                *[(f"allow{i}", pa.bool_()) for i in range(8)],
            ]
        )

        def batches():
            for offset in range(0, len(vectors), 8192):
                block = np.asarray(vectors[offset : offset + 8192])
                flags = permissions[offset : offset + len(block)]
                yield pa.RecordBatch.from_arrays(
                    [
                        pa.array(np.arange(offset, offset + len(block))),
                        pa.FixedSizeListArray.from_arrays(
                            pa.array(block.reshape(-1)), dim
                        ),
                        *[pa.array((flags & (1 << i)) != 0) for i in range(8)],
                    ],
                    schema=schema,
                )
                notify(
                    self,
                    state="arrow-stream-staged",
                    rows_staged=offset + len(block),
                    total_rows=len(vectors),
                )

        self.table = self.db.create_table(
            "documents", data=pa.RecordBatchReader.from_batches(schema, batches())
        )

    def build(self):

        partitions = max(1, round(math_sqrt(self.table.count_rows())))
        self.table.create_index(
            metric="cosine",
            index_type="IVF_HNSW_FLAT",
            num_partitions=partitions,
            m=16,
            ef_construction=200,
        )
        for cell in range(8):
            self.table.create_scalar_index(f"allow{cell}", index_type="BITMAP")
        self.metadata["index"] = {
            "type": "IVF_HNSW_FLAT",
            "partitions": partitions,
            "m": 16,
            "ef_construction": 200,
            "scalar_indexes": 8,
        }

    def candidates(self):
        return [
            {"mode": "ann", "effort": effort, "ef": ef}
            for ef in (40, 200, 400)
            for effort in (0.05, 0.1, 0.25, 0.5, 1.0)
        ] + [{"mode": "exact"}]

    def configure(self, cell, candidate):
        cell_number(cell)

    def search(self, vector, cell, candidate):
        query = (
            self.table.search(vector)
            .distance_type("cosine")
            .where(f"allow{cell_number(cell)} = true", prefilter=True)
            .limit(10)
            .select(["id", "_distance"])
        )
        if candidate["mode"] == "exact":
            return query.bypass_vector_index()
        return query.search_effort(candidate["effort"]).ef(candidate["ef"])

    def query(self, vector, cell, candidate):
        return self.search(vector, cell, candidate).to_arrow().column("id").to_pylist()

    def authorized_count(self, cell):
        return self.table.count_rows(f"allow{cell_number(cell)} = true")

    def global_top10(self, vector):
        return (
            self.table.search(vector)
            .distance_type("cosine")
            .bypass_vector_index()
            .limit(10)
            .select(["id", "_distance"])
            .to_arrow()
            .column("id")
            .to_pylist()
        )

    def explain(self, vector, cell, candidate):
        return self.search(vector, cell, candidate).explain_plan(True)

    def close(self):
        self.db.close()


class RemoteLance:
    def __init__(self):
        self.client = httpx.Client(
            base_url=os.environ["PAPER_LANCE_URL"],
            headers={"Authorization": "Bearer " + os.environ["PAPER_TOKEN"]},
            timeout=120,
        )
        deadline = time.monotonic() + 60
        while True:
            try:
                self.metadata = self.call("/ready", {})
                break
            except httpx.TransportError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "Lance measurement facade readiness deadline"
                    ) from None
                time.sleep(0.1)

    def call(self, path, body, timeout=120):
        response = self.client.post(path, json=body, timeout=timeout)
        response.raise_for_status()
        return response.json()

    def load(self, vectors, permissions):
        self.call("/load", {}, timeout=7200)

    def build(self):
        self.metadata = self.call("/build", {}, timeout=7200)
        self.metadata["query_boundary"] = (
            "persistent HTTP to private Embedded SDK measurement facade, through parsed response"
        )
        self.metadata["measurement_facade"] = (
            "campaign-owned; not counted as a required LanceDB product service"
        )

    def candidates(self):
        return Lance.candidates(self)

    def configure(self, cell, candidate):
        cell_number(cell)

    def query(self, vector, cell, candidate):
        return self.call(
            "/query",
            {
                "vector": vector.tolist(),
                "cell": cell_number(cell),
                "candidate": candidate,
            },
        )

    def explain(self, vector, cell, candidate):
        return self.call(
            "/plan",
            {
                "vector": vector.tolist(),
                "cell": cell_number(cell),
                "candidate": candidate,
            },
        )

    def authorized_count(self, cell):
        return self.call("/count", {"cell": cell_number(cell)})

    def global_top10(self, vector):
        return self.call("/global", {"vector": vector.tolist()})

    def close(self):
        self.client.close()


def math_sqrt(value):
    # Keep the deterministic partition rule independent of training/query data.
    return value**0.5


def connect(kind):
    if kind.startswith("pgvector") or kind == "paradedb":
        return Postgres(kind, os.environ["PAPER_DB_URL"], os.environ["PAPER_PASSWORD"])
    if kind == "antfly":
        return Antfly(os.environ["PAPER_ANTFLY_URL"], os.environ["PAPER_PASSWORD"])
    if kind == "lancedb":
        if "PAPER_LANCE_URL" in os.environ:
            return RemoteLance()
        return Lance(os.environ["PAPER_LANCE_DIR"])
    raise ValueError(kind)
