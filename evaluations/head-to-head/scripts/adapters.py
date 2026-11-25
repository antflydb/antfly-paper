"""Production application boundaries, separate from the acceptance oracle."""

import base64
import json
import re
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
    def __init__(
        self, url, password, *, model_url="http://models:8090", reader_auth="basic"
    ):
        if reader_auth not in ("basic", "api_key"):
            raise ValueError("unknown Antfly reader authentication")
        self.model_url = model_url
        self.url = url
        self.reader_headers = {}
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

            if reader_auth == "api_key":
                key = self.api(
                    "/auth/v1/users/" + tenant + "/api-keys",
                    {"name": "document-reader"},
                    method="POST",
                )
                scheme = "ApiKey"
                credential = key["key_id"] + ":" + key["key_secret"]
            else:
                scheme = "Basic"
                credential = tenant + ":" + reader_password(password, tenant)
            self.reader_headers[tenant] = {
                "Authorization": scheme
                + " "
                + base64.b64encode(credential.encode()).decode()
            }

    def api(self, path, body=None, method=None, tenant=None):
        headers = self.headers if tenant is None else self.reader_headers[tenant]
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
                    "api_url": self.model_url,
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
                        "api_url": self.model_url,
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
        status = index["status"]
        # A physical checkpoint belongs to an owner, not the whole-table
        # aggregate. This fixture explicitly creates exactly one shard.
        shards = {}
        for identity, observed in index.get("shard_status", {}).items():
            fingerprint = observed.get("projection_checkpoint_config_fingerprint")
            shards[identity] = {
                "runtime_present": observed.get("runtime_present") is True,
                "runtime_fresh": observed.get("runtime_fresh") is True,
                "coverage_complete": observed.get("coverage", {}).get("complete")
                is True,
                "observation_complete": observed.get("coverage", {}).get(
                    "observation_complete"
                )
                is True,
                "incarnation": observed.get("incarnation"),
                "checkpoint_status": observed.get("projection_checkpoint_status"),
                "checkpoint_config_hash": int(fingerprint, 16)
                if isinstance(fingerprint, str)
                and re.fullmatch("[0-9a-f]{16}", fingerprint)
                else None,
                "checkpoint_generation": observed.get(
                    "projection_checkpoint_generation"
                ),
            }
        identity, checkpoint = (
            next(iter(shards.items())) if len(shards) == 1 else (None, {})
        )
        incarnation = status.get("incarnation")
        generation = (
            int(incarnation[2:], 16)
            if isinstance(incarnation, str)
            and re.fullmatch("g-[0-9a-f]{16}", incarnation)
            else None
        )
        topology = {
            field: status.get(field)
            for field in (
                "expected_groups",
                "reported_groups",
                "fresh_groups",
                "stale_groups",
                "missing_groups",
                "unknown_remote_groups",
            )
        }
        expected = dict(
            expected_groups=1,
            reported_groups=1,
            fresh_groups=1,
            stale_groups=0,
            missing_groups=0,
            unknown_remote_groups=0,
        )
        authoritative = (
            identity is not None
            and all(
                type(topology[field]) is int and topology[field] == value
                for field, value in expected.items()
            )
            and status.get("runtime_present") is True
            and status.get("runtime_fresh") is True
            and status.get("coverage", {}).get("observation_complete") is True
            and checkpoint.get("runtime_present") is True
            and checkpoint.get("runtime_fresh") is True
            and checkpoint.get("observation_complete") is True
            and checkpoint.get("coverage_complete") is True
            and generation is not None
            and generation > 0
            and checkpoint.get("incarnation") == incarnation
            and checkpoint.get("checkpoint_status") == "clean"
            and checkpoint.get("checkpoint_config_hash") is not None
            and type(checkpoint.get("checkpoint_generation")) is int
            and 0 <= checkpoint["checkpoint_generation"] < 2**64
        )
        return {
            "config": index["config"],
            "coverage_complete": status["coverage"]["complete"],
            "shard_observation_complete": authoritative,
            "topology": topology,
            "shards": shards,
            "checkpoint_shard": identity,
            "index_generation": generation,
            "checkpoint_config_hash": checkpoint.get("checkpoint_config_hash"),
            "checkpoint_generation": checkpoint.get("checkpoint_generation"),
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

    def query(self, tenant, vector, name="a0", text=None, include_vectors=False):
        body = {
            "table": "documents",
            # Kept only for reproducing the historical released-image protocol.
            # Candidate acceptance always uses the separate administrator oracle.
            "fields": ["body", "tenant", "version", "page"]
            + (["_embeddings." + name] if include_vectors else []),
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
