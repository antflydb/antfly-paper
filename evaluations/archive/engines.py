"""Matched page retrieval using precomputed vectors and common rank fusion."""

import base64
import io
import json
import os
import secrets
import time
import zipfile
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.request import Request, urlopen


@dataclass(frozen=True, repr=False)
class ApiKey:
    key_id: str
    key_secret: str


def call(url, method, body=None, auth=None, content_type="application/json"):
    data = (
        body
        if isinstance(body, bytes)
        else None
        if body is None
        else json.dumps(body).encode()
    )
    headers = {"Content-Type": content_type}
    if auth:
        if isinstance(auth, ApiKey):
            scheme, credential = "ApiKey", f"{auth.key_id}:{auth.key_secret}"
        else:
            scheme, credential = "Basic", auth
        headers["Authorization"] = (
            scheme + " " + base64.b64encode(credential.encode()).decode()
        )
    try:
        with urlopen(
            Request(url, method=method, data=data, headers=headers), timeout=180
        ) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except HTTPError as e:
        raise RuntimeError(
            f"{method} {url.split('?')[0]}: {e.code} {e.read(500)!r}"
        ) from e


def filters(q):
    if q["tenant"] == "public":
        return {"tenant": "public"}
    field = q["filter"]["field"]
    if field not in {"review_band", "collection_code", "retention_years"}:
        raise ValueError("unknown tenant field")
    return {"tenant": q["tenant"], field: q["filter"]["equals"]}


class OpenSearch:
    def __init__(self, url, name, *, policy=False):
        self.policy = policy
        self.name = name
        self.url = url.rstrip("/") + "/" + name
        self.auth = os.environ.get("ARCHIVE_OS_AUTH")
        self.readers = None

    def request(self, method, path, body=None):
        return call(self.url + path, method, body, self.auth)

    def create(self, dim):
        self.request(
            "PUT",
            "",
            {
                "settings": {"index.knn": True, "number_of_replicas": 0},
                "mappings": {
                    "dynamic": "strict",
                    "properties": {
                        "tenant": {"type": "keyword"},
                        "page": {"type": "keyword"},
                        "collection_code": {"type": "keyword"},
                        "review_band": {"type": "integer"},
                        "body": {"type": "text"},
                        "vec": {
                            "type": "knn_vector",
                            "dimension": dim,
                            "method": {
                                "engine": "lucene",
                                "name": "hnsw",
                                "space_type": "cosinesimil",
                            },
                        },
                    },
                },
            },
        )

        if self.policy:
            self.create_readers()

    def create_readers(self):
        base = self.url.rsplit("/", 1)[0]
        auth = self.auth
        users = {}
        for tenant in ("archive", "audit", "public"):
            role = self.name + "_" + tenant
            password = "Py!" + secrets.token_hex(20)
            call(
                base + "/_plugins/_security/api/roles/" + role,
                "PUT",
                {
                    "cluster_permissions": [],
                    "index_permissions": [
                        {
                            "index_patterns": [self.name],
                            "allowed_actions": ["read"],
                            "dls": json.dumps({"term": {"tenant": tenant}}),
                        }
                    ],
                },
                auth,
            )
            call(
                base + "/_plugins/_security/api/internalusers/" + role,
                "PUT",
                {"password": password},
                auth,
            )
            call(
                base + "/_plugins/_security/api/rolesmapping/" + role,
                "PUT",
                {"users": [role]},
                auth,
            )
            users[tenant] = role + ":" + password
        self.readers = users

    def insert(self, rows):
        payload = "".join(
            json.dumps({"index": {"_id": r["id"]}})
            + "\n"
            + json.dumps({k: v for k, v in r.items() if k != "id"})
            + "\n"
            for r in rows
        )
        result = call(
            self.url + "/_bulk",
            "POST",
            payload.encode(),
            self.auth,
            "application/x-ndjson",
        )
        if result["errors"]:
            raise RuntimeError(
                f"OpenSearch bulk failure: {next(i for i in result['items'] if i['index']['status'] >= 300)}"
            )

    def finish(self):
        self.request("POST", "/_refresh")

    def evolve(self):
        self.request(
            "PUT", "/_mapping", {"properties": {"retention_years": {"type": "integer"}}}
        )

    def indexed_counts(self):
        return {"rows": self.request("GET", "/_count")["count"]}

    def search(self, q, vector, mode, depth):
        terms = filters(q)
        auth = self.auth
        if self.readers is not None:
            auth = self.readers[terms.pop("tenant")]
        predicate = {"bool": {"filter": [{"term": {k: v}} for k, v in terms.items()]}}
        query = (
            {"bool": {"must": {"match": {"body": q["question"]}}, "filter": predicate}}
            if mode == "lexical"
            else {"knn": {"vec": {"vector": vector, "k": depth, "filter": predicate}}}
        )
        r = call(
            self.url + "/_search",
            "POST",
            {"query": query, "_source": ["page"], "size": depth},
            auth,
        )
        if r["timed_out"] or r["_shards"]["failed"]:
            raise RuntimeError("incomplete OpenSearch response")
        return [(x["_id"], x["_source"]["page"]) for x in r["hits"]["hits"]]


class Antfly:
    def __init__(self, url, name, sync_level="full_index", *, access="basic"):
        self.access = access
        self.sync_level = sync_level
        self.base, self.name = url.rstrip("/"), name
        self.path = "/db/v1/tables/" + name
        self.admin = (
            None if access == "application" else os.environ["ARCHIVE_ANTFLY_AUTH"]
        )
        self.readers = {}

    @staticmethod
    def schema(evolved):
        numeric = {
            "type": "integer",
            "x-antfly-field": {"type": "number", "sortable": True},
        }
        keyword = {"type": "string", "x-antfly-field": {"type": "keyword"}}
        fields = {
            "body": {"type": "string", "x-antfly-field": {"type": "text"}},
            "tenant": keyword,
            "collection_code": keyword,
            "review_band": numeric,
        }
        if evolved:
            fields["retention_years"] = {
                "type": "integer",
                "x-antfly-field": {
                    "type": "number",
                    "sortable": True,
                    "missing_null_policy": "nulls_ordered",
                },
            }
        schema = {
            "default_type": "default",
            "document_schemas": {
                "default": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": True,
                        "properties": fields,
                    }
                }
            },
        }

        if evolved:
            # The nullable sort column proves coverage even for rows without retention.
            schema["index_sort"] = [
                {"field": "retention_years", "order": "asc"},
                {"field": "_id", "order": "asc"},
            ]
        return schema

    def create(self, dim, *, evolved=False):
        self.create_indexes(dim, evolved=evolved)
        if self.access != "application":
            self.create_readers()
        if self.access == "api_key":
            self.readers = {
                t: self.api_key(v.split(":")[0]) for t, v in self.readers.items()
            }
            self.admin = self.api_key(self.admin.split(":")[0])

    def create_indexes(self, dim, *, evolved):
        created = call(
            self.base + self.path,
            "POST",
            {"num_shards": 1, "schema": self.schema(evolved)},
            self.admin,
        )
        self.text_index = self.full_text_index(created)
        call(
            self.base + self.path + "/indexes/vec",
            "POST",
            {
                "type": "embeddings",
                "dimension": dim,
                "external": True,
                "distance_metric": "cosine",
            },
            self.admin,
        )

    def api_key(self, owner):
        r = call(
            self.base + "/auth/v1/users/" + owner + "/api-keys",
            "POST",
            {"name": "archive-fairness"},
            self.admin,
        )
        return ApiKey(r["key_id"], r["key_secret"])

    def create_readers(self):
        for tenant in ("public", "archive", "audit"):
            login, password = self.name + tenant, secrets.token_hex(24)
            call(
                self.base + "/auth/v1/users/" + login,
                "POST",
                {
                    "password": password,
                    "initial_policies": [
                        {
                            "resource": self.name,
                            "resource_type": "table",
                            "type": "read",
                        }
                    ],
                },
                self.admin,
            )
            call(
                self.base + "/auth/v1/users/" + login + "/row-filters/" + self.name,
                "PUT",
                {"term": {"tenant": tenant}},
                self.admin,
            )
            self.readers[tenant] = login + ":" + password

    def insert(self, rows):
        docs = {
            r["id"]: {
                **{k: v for k, v in r.items() if k not in {"id", "vec"}},
                "_embeddings": {"vec": r["vec"]},
            }
            for r in rows
        }
        result = call(
            self.base + self.path + "/batch",
            "POST",
            {"inserts": docs, "sync_level": self.sync_level},
            self.admin,
        )
        if result.get("status") != "committed":
            raise RuntimeError("Antfly batch not committed")

    def finish(self):
        pass

    @staticmethod
    def full_text_index(table):
        return "full_text_index_v" + str(table["schema"]["version"])

    def indexed_counts(self):
        counts = {}
        for name in (self.text_index, "vec"):
            response = call(
                self.base + self.path + "/indexes/" + name, "GET", auth=self.admin
            )
            status = response["status"]
            counts[name] = status["total_indexed"]
        return counts

    def evolve(self):
        table = call(
            self.base + self.path + "/schema", "PUT", self.schema(True), self.admin
        )
        target = self.full_text_index(table)
        # Status can report zero indexed rows until the replacement generation publishes.
        # Use the same full-corpus deadline as the native migration qualification.
        deadline = time.monotonic() + 86400
        while table.get("migration") or self.full_text_index(table) != target:
            if time.monotonic() >= deadline:
                raise TimeoutError("Antfly schema publication: " + target)
            time.sleep(1)
            table = call(self.base + self.path, "GET", auth=self.admin)
        self.text_index = target

    def query(self, q, vector, mode, depth, *, profile=False):
        terms = [
            (
                {
                    "field": k,
                    "min": v,
                    "max": v,
                    "inclusive_min": True,
                    "inclusive_max": True,
                }
                if isinstance(v, int)
                else {"term": {k: v}}
            )
            for k, v in filters(q).items()
            if k != "tenant" or self.access == "application"
        ]
        body = {
            "table": self.name,
            "limit": depth,
            "fields": ["page"],
            "filter_query": {"conjuncts": terms}
            if len(terms) > 1
            else terms[0]
            if terms
            else {"match_all": {}},
        }
        if profile:
            body["profile"] = True
        if mode == "lexical":
            body["full_text_search"] = {"match": q["question"], "field": "body"}
        else:
            body["embeddings"] = {"vec": vector}
        r = call(
            self.base + "/db/v1/query",
            "POST",
            body,
            None if self.access == "application" else self.readers[q["tenant"]],
        )["responses"][0]
        if r.get("status") != 200:
            raise RuntimeError(f"Antfly query failed {r.get('error')}")
        return r

    def search(self, q, vector, mode, depth):
        r = self.query(q, vector, mode, depth)
        return [(x["_id"], x["_source"]["page"]) for x in r["hits"]["hits"]]


class Lance:
    def __init__(self, url, name):
        import lancedb

        self.db, self.name = lancedb.connect(url), name

    def create(self, dim):
        import pyarrow as pa

        self.table = self.db.create_table(
            self.name,
            schema=pa.schema(
                [
                    ("id", pa.string()),
                    ("tenant", pa.string()),
                    ("page", pa.string()),
                    ("body", pa.string()),
                    ("collection_code", pa.string()),
                    ("review_band", pa.int32()),
                    ("vec", pa.list_(pa.float32(), dim)),
                ]
            ),
        )

    def evolve(self):
        self.table.add_columns({"retention_years": "cast(NULL as integer)"})

    def populate_retention(self):
        # The frozen tenant overlay assigns five years plus the metadata bucket.
        for bucket in range(4):
            self.table.update(
                where=f"(tenant='audit' AND review_band={bucket}) OR (tenant='archive' AND collection_code='C{bucket}')",
                values={"retention_years": 5 + bucket},
            )
        return self.table.count_rows()

    def insert(self, rows):
        self.table.add([{k: r.get(k) for k in self.table.schema.names} for r in rows])

    def finish(self):
        self.table.create_fts_index("body", replace=True)
        if self.table.count_rows() >= 256:
            self.table.create_index(
                metric="cosine",
                vector_column_name="vec",
                index_type="IVF_FLAT",
                replace=True,
            )

    def indexed_counts(self):
        return {"rows": self.table.count_rows()}

    def search(self, q, vector, mode, depth):
        expr = " AND ".join(
            k
            + " = "
            + ("'" + v.replace("'", "''") + "'" if isinstance(v, str) else str(v))
            for k, v in filters(q).items()
        )
        query = (
            self.table.search(q["question"], query_type="fts", fts_columns="body")
            if mode == "lexical"
            else self.table.search(vector, vector_column_name="vec").distance_type(
                "cosine"
            )
        )
        return [
            (x["id"], x["page"])
            for x in query.where(expr, prefilter=True)
            .select(["id", "page"])
            .limit(depth)
            .to_list()
        ]


class Vespa:
    """A disposable single-node application. Keep old schema deployed during migration."""

    def __init__(self, url, name, schemas):
        self.url, self.name, self.schemas = url.rstrip("/"), name, schemas
        self.admin = os.environ["ARCHIVE_VESPA_CONFIG"].rstrip("/")

    def create(self, dim):
        self.dim = dim
        self.deploy(False)

    def evolve(self):
        self.deploy(True)

    def deploy(self, retention):
        fields = ""
        for field in ["id", "tenant", "page", "collection_code", "review_band"] + (
            ["retention_years"] if retention else []
        ):
            kind = "int" if field in {"review_band", "retention_years"} else "string"
            fields += f"field {field} type {kind} {{ indexing: attribute | summary }}\n"
        self.schemas[self.name] = f"""schema {self.name} {{
          document {self.name} {{ {fields}
            field body type string {{ indexing: index | summary
                index: enable-bm25 }}
            field vec type tensor<float>(x[{self.dim}]) {{ indexing: attribute | index
                attribute {{ distance-metric: angular }} }} }}
          fieldset default {{ fields: body }}
          rank-profile lexical {{ first-phase {{ expression: bm25(body) }} }}
          rank-profile dense {{ inputs {{ query(q) tensor<float>(x[{self.dim}]) }}
            first-phase {{ expression: closeness(field,vec) }} }} }}"""
        docs = "".join(f'<document type="{n}" mode="index"/>' for n in self.schemas)
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as z:
            z.writestr(
                "hosts.xml",
                '<hosts><host name="localhost"><alias>node1</alias></host></hosts>',
            )
            z.writestr(
                "services.xml",
                f'<services version="1.0"><container id="default" version="1.0"><search/><document-api/><nodes><node hostalias="node1"/></nodes></container><content id="content" version="1.0"><redundancy>1</redundancy><documents>{docs}</documents><nodes><node hostalias="node1" distribution-key="0"/></nodes></content></services>',
            )
            for n, schema in self.schemas.items():
                z.writestr("schemas/" + n + ".sd", schema)
        url = self.admin + "/application/v2/tenant/default"
        call(
            url + "/prepareandactivate",
            "POST",
            data.getvalue(),
            content_type="application/zip",
        )
        deadline = time.monotonic() + 180
        while not call(
            url
            + "/application/default/environment/prod/region/default/instance/default/serviceconverge",
            "GET",
        ).get("converged"):
            if time.monotonic() > deadline:
                raise TimeoutError("Vespa configuration convergence")
            time.sleep(0.5)

    def insert(self, rows):
        from urllib.parse import quote

        for r in rows:
            call(
                f"{self.url}/document/v1/archive/{self.name}/docid/{quote(r['id'], safe='')}",
                "POST",
                {"fields": r},
            )

    def finish(self):
        pass

    def indexed_counts(self):
        root = call(
            self.url + "/search/",
            "POST",
            {"yql": f"select * from {self.name} where true", "hits": 0},
        )["root"]
        if root.get("errors") or root.get("coverage", {}).get("coverage") != 100:
            raise RuntimeError(f"incomplete Vespa count: {root}")
        return {"rows": root["fields"]["totalCount"]}

    def search(self, q, vector, mode, depth):
        params = {}
        predicates = []
        for k, v in filters(q).items():
            if isinstance(v, int):
                predicates.append(f"{k} = {v}")
            else:
                predicates.append(f"{k} contains @f_{k}")
                params["f_" + k] = v
        clause = (
            "userInput(@text)"
            if mode == "lexical"
            else f"({{targetHits:{depth}}}nearestNeighbor(vec,q))"
        )
        body = {
            "yql": f"select id,page from {self.name} where "
            + " and ".join([clause, *predicates]),
            "ranking": mode,
            "hits": depth,
            "timeout": "30s",
            **params,
        }
        if mode == "lexical":
            body["text"] = q["question"]
        else:
            body["input.query(q)"] = vector
        root = call(self.url + "/search/", "POST", body)["root"]
        if root.get("errors"):
            raise RuntimeError(str(root["errors"]))
        return [
            (hit["fields"]["id"], hit["fields"]["page"])
            for hit in root.get("children", [])
        ]


class Pg:
    def __init__(self, url, name):
        import psycopg

        self.writer = psycopg.connect(url, autocommit=True)
        self.match_any_lexeme = False
        self.name = name
        self.readers = {}

    def create(self, dim):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        self.writer.execute("CREATE EXTENSION IF NOT EXISTS vector")
        self.writer.execute(f"""CREATE TABLE {self.name}(id text PRIMARY KEY, tenant text NOT NULL, page text NOT NULL,
            body text NOT NULL, metadata jsonb NOT NULL, vec vector({int(dim)}) NOT NULL,
            lex tsvector GENERATED ALWAYS AS (to_tsvector('english',body)) STORED)""")
        self.writer.execute(f"ALTER TABLE {self.name} ENABLE ROW LEVEL SECURITY")
        self.writer.execute(f"CREATE INDEX ON {self.name} USING gin(lex)")
        self.writer.execute(
            f"CREATE INDEX ON {self.name} USING hnsw(vec vector_cosine_ops)"
        )
        for field in ("collection_code", "review_band"):
            self.writer.execute(
                f"CREATE INDEX ON {self.name} (tenant,(metadata -> '{field}'))"
            )
        for tenant in ("archive", "audit", "public"):
            name, password = self.name + tenant, secrets.token_hex(24)
            role = sql.Identifier(name)
            self.writer.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    role, sql.Literal(password)
                )
            )
            self.writer.execute(
                sql.SQL(f"GRANT SELECT ON {self.name} TO {{}}").format(role)
            )
            self.writer.execute(
                sql.SQL(
                    f"CREATE POLICY {tenant}_read ON {self.name} FOR SELECT TO {{}} USING(tenant={{}})"
                ).format(role, sql.Literal(tenant))
            )
            self.readers[tenant] = psycopg.connect(
                make_conninfo(self.writer.info.dsn, user=name, password=password),
                autocommit=True,
            )

    def evolve(self):
        self.writer.execute(
            f"CREATE INDEX ON {self.name} (tenant,(metadata -> 'retention_years'))"
        )

    def insert(self, rows):
        from psycopg.types.json import Jsonb

        with self.writer.transaction(), self.writer.cursor() as c:
            c.executemany(
                f"""INSERT INTO {self.name}(id,tenant,page,body,vec,metadata) VALUES (%s,%s,%s,%s,%s::vector,%s)
                ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata""",
                [
                    (
                        r["id"],
                        r["tenant"],
                        r["page"],
                        r["body"],
                        str(r["vec"]),
                        Jsonb(
                            {
                                k: r[k]
                                for k in (
                                    "collection_code",
                                    "review_band",
                                    "retention_years",
                                )
                                if k in r
                            }
                        ),
                    )
                    for r in rows
                ],
            )

    def finish(self):
        self.writer.execute(f"ANALYZE {self.name}")

    def indexed_counts(self):
        return {
            "rows": self.writer.execute(f"SELECT count(*) FROM {self.name}").fetchone()[
                0
            ]
        }

    def search(self, q, vector, mode, depth):
        from psycopg.types.json import Jsonb

        args, where = [], "true"
        for k, v in filters(q).items():
            if k != "tenant":
                where += f" AND metadata -> '{k}' = %s"
                args.append(Jsonb(v))
        if mode == "lexical":
            tsquery = "websearch_to_tsquery('english',%s)"
            if self.match_any_lexeme:
                tsquery = """(SELECT coalesce(string_agg(quote_literal(word), ' | '), '')::tsquery
                              FROM unnest(tsvector_to_array(to_tsvector('english',%s))) AS words(word))"""
            where += " AND lex @@ " + tsquery
            order = f"ts_rank_cd(lex,{tsquery}) DESC,id"
            args.extend([q["question"], q["question"]])
        else:
            order = "vec <=> %s::vector"
            args.append(str(vector))
        reader = self.readers[q["tenant"]]
        reader.execute("SET hnsw.iterative_scan = strict_order")
        return reader.execute(
            f"SELECT id,page FROM {self.name} WHERE {where} ORDER BY {order} LIMIT %s",
            [*args, depth],
        ).fetchall()
