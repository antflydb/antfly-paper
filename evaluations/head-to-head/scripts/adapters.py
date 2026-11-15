"""Production application boundaries, separate from the acceptance oracle."""

import base64
import json
import time
from urllib.parse import quote

from common import DETECTOR, EMBEDDER, reader_password, request


def wait(predicate, seconds=180):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise TimeoutError("application readiness deadline")


class Antfly:
    def __init__(self, url, password):
        self.url = url
        self.password = password
        self.headers = {
            "Authorization": "Basic "
            + base64.b64encode(("admin:" + password).encode()).decode()
        }
        self.api("/db/v1/tables/documents", {"num_shards": 1})
        self.generation("a0")
        for tenant in ("alpha", "beta"):
            self.api(
                "/auth/v1/users/" + tenant,
                {
                    "password": reader_password(password, tenant),
                    "initial_policies": [
                        {
                            "resource": "documents",
                            "resource_type": "table",
                            "type": "read",
                        }
                    ],
                },
            )
            self.api(
                "/auth/v1/users/" + tenant + "/row-filters/documents",
                {"term": {"tenant": tenant}},
                method="PUT",
            )

    def api(self, path, body=None, method=None, tenant=None):
        headers = self.headers
        if tenant:
            headers = {
                "Authorization": "Basic "
                + base64.b64encode(
                    (tenant + ":" + reader_password(self.password, tenant)).encode()
                ).decode()
            }
        return request(self.url + path, body, method=method, headers=headers)

    def generation(self, name, model=EMBEDDER):
        self.api(
            "/db/v1/tables/documents/indexes/" + name,
            {
                "type": "embeddings",
                "dimension": 384,
                "field": "body",
                "distance_metric": "cosine",
                "publication_policy": "atomic",
                "embedder": {
                    "provider": "antfly",
                    "model": model,
                    "api_url": "http://models:8090",
                },
            },
        )

    def write(self, identity, row, sync="write"):
        return self.api(
            "/db/v1/tables/documents/batch",
            {"inserts": {identity: row}, "sync_level": sync},
        )

    def delete(self, identity, sync="full_index"):
        return self.api(
            "/db/v1/tables/documents/batch",
            {"deletes": [identity], "sync_level": sync},
        )

    def drop_generation(self, name):
        return self.api("/db/v1/tables/documents/indexes/" + name, method="DELETE")

    def privacy(self):
        return self.api(
            "/db/v1/tables/documents/privacy",
            {
                "mode": "redact_for_search_and_ai",
                "fields": ["body"],
                "category_groups": [],
                "categories": [{"name": "email"}],
                "replacement": "category",
                "producer": {
                    "type": "extractor",
                    "revision": "document-eval-detector-v1",
                    "config": {
                        "provider": "antfly",
                        "model": DETECTOR,
                        "schema_version": 2,
                        "api_url": "http://models:8090",
                    },
                },
            },
            method="PUT",
        )

    def ready(self, name="a0"):
        value = self.api("/db/v1/tables/documents/indexes/" + name)
        return value.get("status", {}).get("coverage", {}).get("complete", False)

    def completion(self, name):
        return self.api("/db/v1/tables/documents/indexes/" + name)["status"]["coverage"]

    def inspection_witness(self, name):
        index = self.api("/db/v1/tables/documents/indexes/" + quote(name, safe=""))
        privacy = self.api("/db/v1/tables/documents/privacy")
        if not index["status"]["coverage"]["complete"]:
            raise ValueError("index is not complete")
        return {
            "config": index["config"],
            "checkpoint_config_hash": int(
                index["status"]["projection_checkpoint_config_fingerprint"], 16
            ),
            "checkpoint_generation": index["status"][
                "projection_checkpoint_generation"
            ],
            "policy_version": privacy["policy_version"],
            "policy_etag": privacy["etag"],
        }

    def inspect_vectors(self, identity, name, tenant=None):
        path = (
            "/db/v1/tables/documents/documents/"
            + quote(identity, safe="")
            + "/artifacts/"
            + quote(name, safe="")
            + "?detail=raw"
        )
        try:
            manifest = self.api(path, tenant=tenant)
        except OSError as exc:
            if "HTTP 404 " in str(exc):
                return None
            raise
        if (
            manifest.get("route_type") != "dense_index_inspection"
            or manifest.get("manifest_version") != 1
        ):
            raise ValueError("unsupported dense index inspection")
        return json.loads(manifest["manifest_json"])

    def query(self, tenant, vector, name="a0", text=None):
        body = {
            "table": "documents",
            "fields": ["body", "tenant", "version", "page"],
            "limit": 10,
            "filter_query": {"match_all": {}},
        }
        if text:
            body["full_text_search"] = {"query": text}
        else:
            body.update(indexes=[name], embeddings={name: vector})
        try:
            value = self.api("/db/v1/query", body, tenant=tenant)["responses"][0]
        except OSError as exc:
            if "HTTP 503 " in str(exc):
                return {"available": False, "hits": []}
            raise
        if value.get("status") != 200:
            if value.get("status") == 503:
                return {"available": False, "hits": []}
            raise ValueError("query rejected: " + json.dumps(value)[:1000])
        return {
            "available": True,
            "hits": [
                {"id": h["_id"], **h["_source"]}
                for h in value.get("hits", {}).get("hits", [])
            ],
        }


class Postgres:
    def __init__(self, url, token, password):
        self.url = url
        self.password = password
        self.headers = {"Authorization": "Bearer " + token}
        self.generation("a0")

    def api(self, path, body):
        return request(self.url + path, body, headers=self.headers)

    def generation(self, name, model=EMBEDDER):
        return self.api("/generation", {"name": name, "model": model})

    def write(self, identity, row, sync="write"):
        result = self.api("/write", {"id": identity, "row": row})
        if sync == "full_index":
            wait(self.ready)
        return result

    def delete(self, identity, sync="write"):
        return self.api("/delete", {"id": identity})

    def drop_generation(self, name):
        return self.api("/drop-generation", {"name": name})

    def privacy(self):
        return self.api("/privacy", {})

    def ready(self, name="a0"):
        return self.api("/ready", {})["complete"]

    def completion(self, name):
        return self.api("/ready", {"name": name})

    def query(self, tenant, vector, name="a0", text=None, include_vectors=False):
        return request(
            self.url + "/query",
            {
                "tenant": "beta" if tenant == "alpha" else "alpha",
                "vector": vector,
                "name": name,
                "text": text,
                "include_vectors": include_vectors,
            },
            headers={
                "Authorization": "Basic "
                + base64.b64encode(
                    (tenant + ":" + reader_password(self.password, tenant)).encode()
                ).decode()
            },
        )
