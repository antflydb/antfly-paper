"""Custom PostgreSQL durable-outbox baseline, not Temporal/Dagster/pgai.

Jobs, source versions and publication guards are persisted in PostgreSQL.
Model calls can repeat before commit; effects are version-conditional. This
is measured application coordination code, not a built-in database feature.
"""

import json
import os
import threading
import time
import urllib.request

import psycopg
from lance_service import serve


class Outbox:
    def __init__(self):
        self.url = os.environ["PAPER_DB_URL"]
        self.lock = threading.RLock()
        self.generations = {}
        with psycopg.connect(self.url) as db:
            db.execute("CREATE EXTENSION IF NOT EXISTS vector")
            db.execute("""CREATE TABLE IF NOT EXISTS sources (id text PRIMARY KEY, body text NOT NULL, version bigint NOT NULL, tag text NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS generations (name text PRIMARY KEY, revision text NOT NULL);
            CREATE TABLE IF NOT EXISTS artifacts (generation text NOT NULL, id text REFERENCES sources(id) ON DELETE CASCADE, version bigint NOT NULL, vector vector(2) NOT NULL, PRIMARY KEY(generation,id));
            CREATE TABLE IF NOT EXISTS jobs (generation text NOT NULL, id text NOT NULL, version bigint NOT NULL, body text NOT NULL, state text NOT NULL DEFAULT 'pending', attempts integer NOT NULL DEFAULT 0, PRIMARY KEY(generation,id,version));""")
        threading.Thread(target=self.worker, daemon=True).start()

    def generation(self, name, revision):
        with psycopg.connect(self.url) as db:
            db.execute("INSERT INTO generations VALUES (%s,%s)", (name, revision))
            db.execute(
                "INSERT INTO jobs(generation,id,version,body) SELECT %s,id,version,body FROM sources",
                (name,),
            )
        return {"ok": True}

    def write(self, identity, row):
        with psycopg.connect(self.url) as db:
            old = db.execute(
                "SELECT body,version FROM sources WHERE id=%s FOR UPDATE", (identity,)
            ).fetchone()
            if old and row["version"] < old[1]:
                raise ValueError("source version cannot move backward")
            db.execute(
                "INSERT INTO sources VALUES (%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET body=EXCLUDED.body,version=EXCLUDED.version,tag=EXCLUDED.tag",
                (identity, row["body"], row["version"], row.get("tag", "")),
            )
            if not old or old[0] != row["body"]:
                db.execute(
                    "INSERT INTO jobs(generation,id,version,body) SELECT name,%s,%s,%s FROM generations ON CONFLICT DO NOTHING",
                    (identity, row["version"], row["body"]),
                )
        return {"ok": True}

    def metadata(self, identity, row):
        with psycopg.connect(self.url) as db:
            db.execute("UPDATE sources SET tag=%s WHERE id=%s", (row["tag"], identity))
        return {"ok": True}

    def delete(self, identity):
        with psycopg.connect(self.url) as db:
            db.execute("DELETE FROM sources WHERE id=%s", (identity,))
        return {"ok": True}

    def query(self, name):
        with psycopg.connect(self.url) as db:
            revision = db.execute(
                "SELECT revision FROM generations WHERE name=%s", (name,)
            ).fetchone()[0]
            rows = db.execute(
                "SELECT a.id,a.vector::text FROM artifacts a JOIN sources s ON s.id=a.id WHERE a.generation=%s ORDER BY a.vector <=> %s::vector LIMIT 100",
                (name, "[1,0]" if revision == "A" else "[0,1]"),
            ).fetchall()
        return {k: json.loads(v) for k, v in rows}

    def complete(self, name):
        with psycopg.connect(self.url) as db:
            result = db.execute(
                "SELECT NOT EXISTS(SELECT 1 FROM sources s LEFT JOIN artifacts a ON a.id=s.id AND a.generation=%s AND a.version=s.version WHERE a.id IS NULL)",
                (name,),
            ).fetchone()[0]
        return {"complete": result}

    def worker(self):
        while True:
            selected = None
            try:
                with psycopg.connect(self.url) as db:
                    job = db.execute(
                        "SELECT j.generation,j.id,j.version,j.body,g.revision FROM jobs j JOIN generations g ON g.name=j.generation WHERE j.state='pending' AND j.attempts<5 ORDER BY j.version FOR UPDATE OF j SKIP LOCKED LIMIT 1"
                    ).fetchone()
                    if job:
                        selected = job[:3]
                        gen, identity, version, body, revision = job
                        request = urllib.request.Request(
                            f"http://provider-{revision.lower()}:8090/v1/embeddings",
                            data=json.dumps({"input": [body]}).encode(),
                            headers={"Content-Type": "application/json"},
                        )
                        with urllib.request.urlopen(request, timeout=65) as response:
                            value = json.load(response)
                        expected = ("a" if revision == "A" else "b") * 64
                        if value.get("model_identity") != expected:
                            raise ValueError(
                                "provider revision does not match generation"
                            )
                        current = db.execute(
                            "SELECT body,version FROM sources WHERE id=%s FOR UPDATE",
                            (identity,),
                        ).fetchone()
                        if current == (body, version):
                            vector = json.dumps(value["data"][0]["embedding"])
                            db.execute(
                                "INSERT INTO artifacts VALUES (%s,%s,%s,%s::vector) ON CONFLICT(generation,id) DO UPDATE SET version=EXCLUDED.version,vector=EXCLUDED.vector",
                                (gen, identity, version, vector),
                            )
                            state = "published"
                        else:
                            state = "superseded"
                        db.execute(
                            "UPDATE jobs SET state=%s,attempts=attempts+1 WHERE generation=%s AND id=%s AND version=%s",
                            (state, *selected),
                        )
            except Exception as exc:
                print("worker retry: " + type(exc).__name__, flush=True)
                if selected:
                    with psycopg.connect(self.url) as db:
                        db.execute(
                            "UPDATE jobs SET attempts=attempts+1 WHERE generation=%s AND id=%s AND version=%s",
                            selected,
                        )
            time.sleep(0.02)


if __name__ == "__main__":
    # Reuse only the authenticated instrumentation boundary; the application
    # implementation and its durable state are entirely PostgreSQL-backed.
    import lance_service

    lance_service.Application = Outbox
    serve().serve_forever()
