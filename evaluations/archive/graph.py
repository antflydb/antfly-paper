"""Exercise page-supported assertions on two actual storage engines."""

import argparse
import hashlib
import json
import os
import re
import secrets
import uuid
from pathlib import Path

from corpus import extract_pdf, fetch_object
from engines import call
from evidence import score_assertions


class AntflyGraph:
    def __init__(self, url, name):
        self.url, self.name = url.rstrip("/"), name
        self.admin = os.environ["ARCHIVE_ANTFLY_AUTH"]
        self.path = self.url + "/db/v1/tables/" + name
        props = {
            t: {"type": "string", "x-antfly-types": ["keyword"]}
            for t in ("audit", "archive")
        }
        schema = {
            "default_type": "doc",
            "document_schemas": {
                "doc": {
                    "schema": {
                        "type": "object",
                        "properties": props,
                        "additionalProperties": True,
                    }
                }
            },
        }
        call(self.path, "POST", {"num_shards": 1, "schema": schema}, self.admin)
        call(
            self.path + "/indexes/predicates",
            "POST",
            {"name": "predicates", "type": "algebraic", "derive_from_schema": True},
            self.admin,
        )
        call(
            self.path + "/indexes/links",
            "POST",
            {
                "name": "links",
                "type": "graph",
                "edge_types": [
                    {"name": "points_to", "field": "target", "topology": "tree"}
                ],
            },
            self.admin,
        )
        call(
            self.path + "/indexes/vec",
            "POST",
            {
                "type": "embeddings",
                "dimension": 2,
                "distance_metric": "cosine",
                "external": True,
            },
            self.admin,
        )
        self.readers = {}
        for tenant in ("audit", "archive"):
            login, password = name + tenant, secrets.token_hex(24)
            call(
                self.url + "/auth/v1/users/" + login,
                "POST",
                {
                    "password": password,
                    "initial_policies": [
                        {"resource": name, "resource_type": "table", "type": "read"}
                    ],
                },
                self.admin,
            )
            call(
                self.url + "/auth/v1/users/" + login + "/row-filters/" + name,
                "PUT",
                {"term": {tenant: "yes"}},
                self.admin,
            )
            self.readers[tenant] = login + ":" + password

    def put(self, key, target, audit):
        doc = {
            "archive": "yes",
            "audit": "yes" if audit else "no",
            "body": key,
            "_embeddings": {"vec": [1.0, 0.0] if key == "harbor" else [0.0, 1.0]},
        }
        if target:
            doc["target"] = target
        call(
            self.path + "/batch",
            "POST",
            {"inserts": {key: doc}, "sync_level": "full_index"},
            self.admin,
        )

    def seed(self, tenant):
        query = {
            "table": self.name,
            "full_text_search": {"match": "harbor", "field": "body"},
            "embeddings": {"vec": [1.0, 0.0]},
            "limit": 1,
            "fields": [],
        }
        r = call(self.url + "/db/v1/query", "POST", query, self.readers[tenant])[
            "responses"
        ][0]
        return r["hits"]["hits"][0]["_id"]

    def paths(self, tenant):
        anchor = self.seed(tenant)
        match = {
            "anchor": "agency",
            "nodes": {"agency": {"filter": {"ids": [anchor]}}, "item": {}, "page": {}},
            "edges": [
                {"from": "item", "to": "agency", "types": ["points_to"]},
                {"from": "page", "to": "item", "types": ["points_to"]},
            ],
        }
        body = {
            "table": self.name,
            "graph_queries": {
                "facts": {
                    "index": "links",
                    "match": match,
                    "return": {"bindings": ["agency", "item", "page"], "limit": 50},
                }
            },
        }
        response = call(self.url + "/db/v1/query", "POST", body, self.readers[tenant])[
            "responses"
        ][0]
        if response.get("status") != 200:
            raise RuntimeError(str(response.get("error")))
        result = response["graph_results"]["facts"]
        return [
            (r["agency"]["key"], "paid-for", r["item"]["key"], r["page"]["key"])
            for r in result.get("rows", [])
        ]


class PgGraph:
    def __init__(self, url, name):
        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        self.writer = psycopg.connect(url, autocommit=True)
        self.name = name
        self.writer.execute("CREATE EXTENSION IF NOT EXISTS vector")
        self.writer.execute(
            f"CREATE TABLE {name}(id text PRIMARY KEY,target text, audit boolean NOT NULL, vec vector(2) NOT NULL)"
        )
        self.writer.execute(f"CREATE INDEX ON {name}(target)")
        self.writer.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
        self.readers = {}
        for tenant in ("audit", "archive"):
            role, password = name + tenant, secrets.token_hex(24)
            self.writer.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )
            self.writer.execute(f"GRANT SELECT ON {name} TO {role}")
            self.writer.execute(
                f"CREATE POLICY {tenant}_read ON {name} FOR SELECT TO {role} USING ({'true' if tenant == 'archive' else 'audit'})"
            )
            self.readers[tenant] = psycopg.connect(
                make_conninfo(url, user=role, password=password), autocommit=True
            )

    def put(self, key, target, audit):
        self.writer.execute(
            f"""INSERT INTO {self.name} VALUES (%s,%s,%s,%s::vector) ON CONFLICT(id)
            DO UPDATE SET target=excluded.target,audit=excluded.audit""",
            (key, target, audit, "[1,0]" if key == "harbor" else "[0,1]"),
        )

    def seed(self, tenant):
        return (
            self.readers[tenant]
            .execute(f"""SELECT id FROM {self.name}
            ORDER BY ts_rank_cd(to_tsvector('english',id),plainto_tsquery('english','harbor')) +
                     (1-(vec <=> '[1,0]'::vector)) DESC, id LIMIT 1""")
            .fetchone()[0]
        )

    def paths(self, tenant):
        anchor = self.seed(tenant)
        return (
            self.readers[tenant]
            .execute(
                f"""SELECT a.id,'paid-for',i.id,p.id FROM {self.name} a
            JOIN {self.name} i ON i.target=a.id JOIN {self.name} p ON p.target=i.id WHERE a.id=%s""",
                (anchor,),
            )
            .fetchall()
        )


def run(args):
    root = args.fixture
    labels = json.loads((root / "graph-gold.json").read_text())
    refs = json.loads((root / "objects.json").read_text())
    name = "graph" + uuid.uuid4().hex[:12]
    graph = (PgGraph if args.engine == "postgres" else AntflyGraph)(args.endpoint, name)
    for key, target in (("harbor", None), ("lamps", "harbor"), ("rope", "harbor")):
        graph.put(key, target, True)
    results = {}

    def check(phase, tenant, gold):
        result = score_assertions(gold, graph.paths(tenant))
        results[phase + "-" + tenant] = result

    frontend = []
    for ref in refs:
        if args.objects:
            from urllib.error import HTTPError
            from urllib.request import Request, urlopen

            url = args.objects.rstrip("/") + "/current/" + ref["key"]
            if ref["version"] > 1:
                old = next(
                    x
                    for x in refs
                    if x["key"] == ref["key"] and x["version"] == ref["version"] - 1
                )
                headers = {
                    "If-Match": '"' + old["sha256"] + '"',
                    "X-Fixture-Version": "/" + ref["path"],
                }
                with urlopen(Request(url, method="PUT", headers=headers), timeout=10):
                    pass
                try:
                    fetch_object(url, old["sha256"], headers["If-Match"])
                except HTTPError as exc:
                    if exc.code != 412:
                        raise
                else:
                    raise AssertionError("changed object accepted old version")
            raw = fetch_object(url, ref["sha256"], '"' + ref["sha256"] + '"')
        else:
            raw = (root / ref["path"]).read_bytes()
            if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
                raise ValueError("fixture object changed")
        if args.models:
            pages = extract_pdf(raw, args.models)
            if len(pages) != 1:
                raise ValueError("fixture page count")
            body = pages[0]["body"]
            matches = {
                "lamps" if term in ("lamp", "lamps") else term
                for term in re.findall(r"\b(lamps?|rope)\b", body.lower())
            }
            if len(matches) != 1 or "harbor" not in body.lower():
                raise ValueError(
                    "could not extract unambiguous supported fixture assertion"
                )
            item = matches.pop()
            frontend.append(
                {
                    "key": ref["key"],
                    "sha256": ref["sha256"],
                    "extraction": pages[0]["extraction"],
                    "bytes": len(raw),
                }
            )
        else:
            # Matched assertions isolate query and update semantics from OCR quality.
            item = "lamps" if ref["version"] == 1 else "rope"
        graph.put(ref["key"] + "#page=1", item, True)
        if ref["key"] == "appendix.pdf":
            for t in ("archive", "audit"):
                check("original", t, labels["original"])
    for t in ("archive", "audit"):
        check("corrected", t, labels["corrected"])
    graph.put("rope", "harbor", False)
    check("revoked-intermediate", "audit", labels["audit_after_revoke"])
    check("revoked-intermediate", "archive", labels["corrected"])
    outcome = {
        "engine": args.engine,
        "frontend": frontend,
        "checks": results,
        "input": "shared PDF extraction"
        if args.models
        else "matched authored assertions",
    }
    args.out.write_text(json.dumps(outcome, indent=2))
    return int(any(x["missing"] or x["unexpected"] for x in results.values()))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("engine", choices=["antfly", "postgres"])
    p.add_argument("--endpoint", required=True)
    p.add_argument("--fixture", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--objects")
    p.add_argument("--models")
    raise SystemExit(run(p.parse_args()))
