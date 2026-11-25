"""Content-addressed inference results for offline PDF preparation."""

import hashlib
import json
import math
import os
import sqlite3
import struct
from collections import Counter
from contextlib import contextmanager

from pdf_vector_compare import CHUNK_POLICY, MODEL, embed_batch, passage_vectors

DIMENSIONS = 768
VECTOR = struct.Struct(f"<{DIMENSIONS}d")
DOCUMENT = "RETRIEVAL_DOCUMENT"


def digest(data):
    return hashlib.sha256(data).digest()


class PreparationCache:
    def __init__(self, conn, runtime):
        self.db = conn
        self.runtime = runtime
        self.counts = Counter()
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS preparation_v1 (key BLOB PRIMARY KEY, value BLOB NOT NULL, checksum BLOB NOT NULL) WITHOUT ROWID"
        )

    def key(self, identity, task, kind, payload):
        recipe = json.dumps(
            [self.runtime, MODEL, identity, task, DIMENSIONS, kind],
            separators=(",", ":"),
        ).encode()
        return digest(recipe + digest(payload))

    def vector_key(self, identity, task, item):
        if isinstance(item, str):
            return self.key(identity, task, "text", item.encode())
        # Include all structured fields and the exact data URL supplied to the model.
        return self.key(
            identity,
            task,
            "content",
            json.dumps(item, sort_keys=True, separators=(",", ":")).encode(),
        )

    def get(self, key):
        row = self.db.execute(
            "SELECT value, checksum FROM preparation_v1 WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return None
        value, checksum = row
        if digest(key + value) != checksum:
            raise ValueError("embedding cache checksum mismatch")
        return value

    def put(self, key, value):
        return self.db.execute(
            "INSERT OR IGNORE INTO preparation_v1 VALUES (?,?,?)",
            (key, value, digest(key + value)),
        )

    def embed(self, endpoint, inputs, task, identity):
        if not identity:
            raise ValueError("a verified model identity is required for cache lookup")
        keys = [self.vector_key(identity, task, item) for item in inputs]
        found = {}
        missing = {}
        for key, item in zip(keys, inputs, strict=True):
            if key in found or key in missing:
                continue
            value = self.get(key)
            if value is None:
                missing[key] = item
            else:
                found[key] = list(VECTOR.unpack(value))
        self.counts[task + "_hits"] += len(inputs) - len(missing)
        if missing:
            vectors, _ = embed_batch(endpoint, list(missing.values()), task, identity)
            with self.db:
                for key, vector in zip(missing, vectors, strict=True):
                    inserted = self.put(key, VECTOR.pack(*vector)).rowcount
                    found[key] = (
                        vector if inserted else list(VECTOR.unpack(self.get(key)))
                    )
            self.counts[task + "_computed"] += len(missing)
        return [found[key] for key in keys], identity

    def passages(self, endpoint, body, identity):
        if not body.strip():
            return
        policy = json.dumps(CHUNK_POLICY, sort_keys=True)
        key = self.key(identity, DOCUMENT, "page-plan-v1:" + policy, body.encode())
        saved = self.get(key)
        if saved is None:
            texts = [
                text
                for text, _ in passage_vectors(endpoint, body, identity, execute=False)
            ]
            with self.db:
                self.put(key, json.dumps(texts, ensure_ascii=False).encode())
            self.counts["page_plans_computed"] += 1
        else:
            texts = json.loads(saved)
            self.counts["page_plan_hits"] += 1
        for start in range(0, len(texts), 16):
            batch = texts[start : start + 16]
            vectors, _ = self.embed(endpoint, batch, DOCUMENT, identity)
            yield from zip(batch, vectors, strict=True)

    def seed_vector(self, identity, task, item, vector):
        if len(vector) != DIMENSIONS or not all(map(math.isfinite, vector)):
            raise ValueError("invalid vector in prepared inputs")
        self.put(self.vector_key(identity, task, item), VECTOR.pack(*vector))

    def seed_queries(self, path, prompts, identity):
        saved = json.loads(path.read_text())
        with self.db:
            for row, prompt in zip(saved, prompts, strict=True):
                if {
                    k: v for k, v in row.items() if k not in ("a", "b")
                } != prompt or row["a"] != row["b"]:
                    raise ValueError(
                        "frozen query inventory differs from current workload"
                    )
                self.seed_vector(
                    identity, "RETRIEVAL_QUERY", prompt["question"], row["a"]
                )

    def seed_passages(self, path, shard_sha, identity):
        # Historical image shards do not record the exact rendered bytes. Only import text vectors.
        marker = self.key(
            identity, DOCUMENT, "verified-passage-shard-v1", bytes.fromhex(shard_sha)
        )
        if self.get(marker) is not None:
            return
        with path.open() as stream, self.db:
            for line in stream:
                row = json.loads(line)
                self.seed_vector(identity, DOCUMENT, row["body"], row["b"])
            self.put(marker, b"complete")
        self.counts["historical_passage_shards_imported"] += 1


@contextmanager
def open_cache(path, runtime):
    if path is None:
        yield None
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
    conn = sqlite3.connect(path, timeout=120)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield PreparationCache(conn, runtime)
    finally:
        conn.close()
