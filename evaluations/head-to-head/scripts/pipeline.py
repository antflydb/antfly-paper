"""Application-owned durable publication, privacy and RLS for PostgreSQL.

One source transaction enqueues immutable jobs. A worker may repeat inference
after a crash; publication requires the current source and policy generations.
"""

import base64
import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
from common import embed, findings, reader_password

URL = os.environ["PAPER_DB_URL"]
MODELS = "http://models:8090"


def initialize():
    with psycopg.connect(URL) as db:
        db.execute("CREATE EXTENSION IF NOT EXISTS vector")
        db.execute("""CREATE TABLE IF NOT EXISTS source(id text PRIMARY KEY, body text NOT NULL, tenant text NOT NULL, version bigint NOT NULL, page integer NOT NULL);
        CREATE TABLE IF NOT EXISTS generation(name text PRIMARY KEY, model text NOT NULL);
        CREATE TABLE IF NOT EXISTS policy(singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), epoch bigint NOT NULL, enabled boolean NOT NULL);
        INSERT INTO policy VALUES(true,0,false) ON CONFLICT DO NOTHING;
        CREATE TABLE IF NOT EXISTS artifact(generation text NOT NULL, id text REFERENCES source(id) ON DELETE CASCADE, version bigint NOT NULL, epoch bigint NOT NULL, tenant text NOT NULL, body text NOT NULL, page integer NOT NULL, embedding vector(384) NOT NULL, PRIMARY KEY(generation,id));
        CREATE TABLE IF NOT EXISTS job(generation text NOT NULL, id text NOT NULL, version bigint NOT NULL, epoch bigint NOT NULL, body text NOT NULL, state text NOT NULL DEFAULT 'pending', attempts integer NOT NULL DEFAULT 0, PRIMARY KEY(generation,id,version,epoch));
        DO $$ BEGIN IF NOT EXISTS(SELECT FROM pg_roles WHERE rolname='document_reader') THEN CREATE ROLE document_reader NOLOGIN NOSUPERUSER NOBYPASSRLS; END IF; END $$;
        GRANT SELECT ON artifact TO document_reader;
        ALTER TABLE artifact ENABLE ROW LEVEL SECURITY;
        ALTER TABLE artifact FORCE ROW LEVEL SECURITY;
        DROP POLICY IF EXISTS tenant_read ON artifact;
        CREATE POLICY tenant_read ON artifact TO document_reader USING (tenant=current_user);""")
        for tenant in ("alpha", "beta"):
            if not db.execute(
                "SELECT 1 FROM pg_roles WHERE rolname=%s", (tenant,)
            ).fetchone():
                db.execute(
                    psycopg.sql.SQL(
                        "CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS IN ROLE document_reader PASSWORD {}"
                    ).format(
                        psycopg.sql.Identifier(tenant),
                        psycopg.sql.Literal(
                            reader_password(os.environ["PAPER_PASSWORD"], tenant)
                        ),
                    )
                )


def enqueue(db):
    db.execute("""INSERT INTO job(generation,id,version,epoch,body)
        SELECT g.name,s.id,s.version,p.epoch,s.body FROM source s CROSS JOIN generation g CROSS JOIN policy p
        ON CONFLICT DO NOTHING""")


def sanitize(text):
    result = findings(text, MODELS)
    entities = result["data"][0].get("entities", [])
    raw = text.encode()
    spans = set()
    for entity in entities:
        if entity["label"] != "email":
            continue
        literal = entity["text"].encode()
        if not literal:
            raise ValueError("empty entity")
        offset = 0
        while True:
            start = raw.find(literal, offset)
            if start < 0:
                break
            spans.add((start, start + len(literal)))
            offset = start + len(literal)
    spans = sorted(spans, reverse=True)
    for start, end in spans:
        if not 0 <= start < end <= len(raw):
            raise ValueError("invalid extractor byte span")
        raw = raw[:start] + b"[email]" + raw[end:]
    return raw.decode()


def worker():
    while True:
        selected = None
        try:
            with psycopg.connect(URL) as db:
                job = db.execute("""SELECT j.generation,j.id,j.version,j.epoch,j.body,g.model,p.enabled
                    FROM job j JOIN generation g ON g.name=j.generation CROSS JOIN policy p
                    WHERE j.state='pending' AND j.attempts<5 ORDER BY j.version,j.id
                    FOR UPDATE OF j SKIP LOCKED LIMIT 1""").fetchone()
                if job:
                    gen, identity, version, epoch, body, model, protected = job
                    selected = job[:4]
                    reusable = db.execute(
                        """SELECT a.body,a.embedding::text FROM artifact a
                        JOIN generation g ON g.name=a.generation
                        JOIN job j ON j.generation=a.generation AND j.id=a.id AND j.version=a.version AND j.epoch=a.epoch
                        WHERE a.id=%s AND a.epoch=%s AND g.model=%s AND j.body=%s AND j.state='published' LIMIT 1""",
                        (identity, epoch, model, body),
                    ).fetchone()
                    if reusable:
                        safe, vector = reusable[0], json.loads(reusable[1])
                    else:
                        safe = sanitize(body) if protected else body
                        vector = embed([safe], MODELS, model)[0]
                    current = db.execute(
                        "SELECT body,version,tenant,page FROM source WHERE id=%s FOR UPDATE",
                        (identity,),
                    ).fetchone()
                    policy = db.execute(
                        "SELECT epoch FROM policy FOR SHARE"
                    ).fetchone()[0]
                    if current and current[:2] == (body, version) and policy == epoch:
                        db.execute(
                            """INSERT INTO artifact VALUES(%s,%s,%s,%s,%s,%s,%s,%s::vector)
                            ON CONFLICT(generation,id) DO UPDATE SET version=EXCLUDED.version,epoch=EXCLUDED.epoch,
                            tenant=EXCLUDED.tenant,body=EXCLUDED.body,page=EXCLUDED.page,embedding=EXCLUDED.embedding""",
                            (
                                gen,
                                identity,
                                version,
                                epoch,
                                current[2],
                                safe,
                                current[3],
                                json.dumps(vector),
                            ),
                        )
                        state = "published"
                    else:
                        state = "superseded"
                    db.execute(
                        "UPDATE job SET state=%s,attempts=attempts+1 WHERE generation=%s AND id=%s AND version=%s AND epoch=%s",
                        (state, *selected),
                    )
        except Exception as exc:
            print("worker: " + type(exc).__name__, flush=True)
            if selected:
                with psycopg.connect(URL) as db:
                    db.execute(
                        "UPDATE job SET attempts=attempts+1 WHERE generation=%s AND id=%s AND version=%s AND epoch=%s",
                        selected,
                    )
        time.sleep(0.02)


def execute(path, body):
    with psycopg.connect(URL) as db:
        if path == "/write":
            row = body["row"]
            old = db.execute(
                "SELECT version FROM source WHERE id=%s FOR UPDATE", (body["id"],)
            ).fetchone()
            if old and row["version"] < old[0]:
                raise ValueError("version regression")
            db.execute(
                "INSERT INTO source VALUES(%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET body=EXCLUDED.body,tenant=EXCLUDED.tenant,version=EXCLUDED.version,page=EXCLUDED.page",
                (body["id"], row["body"], row["tenant"], row["version"], row["page"]),
            )
            # Restriction changes apply immediately even while vectors lag.
            db.execute(
                "UPDATE artifact SET tenant=%s WHERE id=%s", (row["tenant"], body["id"])
            )
            enqueue(db)
        elif path == "/delete":
            db.execute("DELETE FROM source WHERE id=%s", (body["id"],))
        elif path == "/generation":
            db.execute(
                "INSERT INTO generation VALUES(%s,%s)", (body["name"], body["model"])
            )
            enqueue(db)
        elif path == "/drop-generation":
            db.execute("DELETE FROM job WHERE generation=%s", (body["name"],))
            db.execute("DELETE FROM artifact WHERE generation=%s", (body["name"],))
            db.execute("DELETE FROM generation WHERE name=%s", (body["name"],))
        elif path == "/privacy":
            db.execute("UPDATE policy SET epoch=epoch+1,enabled=true")
            enqueue(db)
        elif path == "/ready":
            return {
                "complete": db.execute("""SELECT NOT EXISTS(SELECT FROM source s CROSS JOIN generation g CROSS JOIN policy p
                LEFT JOIN artifact a ON a.id=s.id AND a.generation=g.name AND a.version=s.version AND a.epoch=p.epoch WHERE a.id IS NULL)""").fetchone()[
                    0
                ],
                "pending": db.execute(
                    "SELECT count(*) FROM job WHERE state='pending'"
                ).fetchone()[0],
            }
        elif path == "/query":
            epoch = db.execute("SELECT epoch FROM policy FOR SHARE").fetchone()[0]
            # Fail closed across the protected table while policy backfill is pending.
            if db.execute(
                "SELECT EXISTS(SELECT FROM source s LEFT JOIN artifact a ON a.id=s.id AND a.generation=%s AND a.epoch=%s WHERE a.id IS NULL)",
                (body["name"], epoch),
            ).fetchone()[0]:
                return {"available": False, "hits": []}
            connection = psycopg.conninfo.conninfo_to_dict(URL)
            connection.update(
                user=body["principal"],
                password=reader_password(
                    os.environ["PAPER_PASSWORD"], body["principal"]
                ),
            )
            reader = psycopg.connect(**connection)
            if body.get("text"):
                rows = reader.execute(
                    "SELECT id,body,page,version FROM artifact WHERE generation=%s AND epoch=%s AND to_tsvector('english',body) @@ plainto_tsquery('english',%s) LIMIT 10",
                    (body["name"], epoch, body["text"]),
                ).fetchall()
            else:
                rows = reader.execute(
                    "SELECT id,body,page,version,embedding::text FROM artifact WHERE generation=%s AND epoch=%s ORDER BY embedding <=> %s::vector LIMIT 10",
                    (body["name"], epoch, json.dumps(body["vector"])),
                ).fetchall()
            reader.close()
            return {
                "available": True,
                "hits": [
                    {
                        "id": r[0],
                        "body": r[1],
                        "page": r[2],
                        "version": r[3],
                        **(
                            {"_embeddings": {body["name"]: json.loads(r[4])}}
                            if len(r) > 4 and body.get("include_vectors")
                            else {}
                        ),
                    }
                    for r in rows
                ],
            }
        else:
            raise ValueError("unknown application operation")
    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        code = 200
        try:
            principal = None
            if self.path == "/query":
                header = self.headers.get("Authorization", "")
                if not header.startswith("Basic "):
                    raise PermissionError()
                principal, password = (
                    base64.b64decode(header[6:], validate=True).decode().split(":", 1)
                )
                if principal not in {"alpha", "beta"} or not hmac.compare_digest(
                    password, reader_password(os.environ["PAPER_PASSWORD"], principal)
                ):
                    raise PermissionError()
            elif not hmac.compare_digest(
                self.headers.get("Authorization", ""),
                "Bearer " + os.environ["PAPER_TOKEN"],
            ):
                raise PermissionError()
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= 1024 * 1024:
                raise ValueError("request size")
            body = json.loads(self.rfile.read(size))
            if principal:
                body["principal"] = principal
            value = execute(self.path, body)
        except Exception as exc:
            code = 403 if isinstance(exc, PermissionError) else 500
            value = {"error": type(exc).__name__}
        raw = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


if __name__ == "__main__":
    initialize()
    threading.Thread(target=worker, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8091), Handler).serve_forever()
