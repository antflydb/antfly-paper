"""Native OpenSearch semantic fields with ML Commons remote connectors.

The providers are private controlled fixtures, not OpenAI or hosted services.
New model/index identities implement supported replacement generations.
"""

IMAGE = "opensearchproject/opensearch@sha256:bbdf65cb3d35c61a8ba495f22c1a42d7d7f0df23412e3419bf1e6cfca3ba4774"
VERSION = "3.9.0"


class OpenSearch:
    supports_async = False

    def __init__(self, owned, call, wait, docker):
        self.owned, self.call, self.wait, self.docker = owned, call, wait, docker
        self.revisions = {}
        self.sources = {}
        owned.environment.update(
            {
                "discovery.type": "single-node",
                "DISABLE_SECURITY_PLUGIN": "true",
                "DISABLE_INSTALL_DEMO_CONFIG": "true",
                "OPENSEARCH_JAVA_OPTS": "-Xms1g -Xmx1g",
                "node.store.allow_mmap": "false",
            }
        )
        volume = owned.volume("opensearch-data")
        self.name = owned.container(
            "opensearch",
            IMAGE,
            [],
            env=[
                "discovery.type",
                "DISABLE_SECURITY_PLUGIN",
                "DISABLE_INSTALL_DEMO_CONFIG",
                "OPENSEARCH_JAVA_OPTS",
                "node.store.allow_mmap",
            ],
            mounts=[f"type=volume,src={volume},dst=/usr/share/opensearch/data"],
            ports=[9200],
            ulimits=["nofile=65536:524288"],
        )
        self.refresh()
        self.ready()
        self.api(
            "/_cluster/settings",
            "PUT",
            {
                "persistent": {
                    "plugins.ml_commons.only_run_on_ml_node": False,
                    "plugins.ml_commons.connector.private_ip_enabled": True,
                    "plugins.ml_commons.trusted_connector_private_endpoints_regex": [
                        "^http://provider-[ab]:8090/v1/embeddings$"
                    ],
                    "plugins.ml_commons.trusted_connector_endpoints_regex": [
                        "^http://provider-[ab]:8090/v1/embeddings$"
                    ],
                }
            },
        )

    def refresh(self):
        self.url = f"http://127.0.0.1:{self.owned.port(self.name, 9200)}"

    def api(self, path, method="GET", body=None, timeout=40):
        return self.call(self.url + path, method, body, timeout)

    def ready(self):
        def probe():
            try:
                return self.api("/").get("version", {}).get("number") == VERSION
            except (OSError, RuntimeError):
                return False

        self.wait(probe, 180)

    def task(self, result):
        if "task_id" not in result:
            return result

        def done():
            status = self.api("/_plugins/_ml/tasks/" + result["task_id"])
            if status.get("state") == "FAILED":
                raise RuntimeError(
                    "OpenSearch ML task failed: " + str(status.get("error", ""))[:600]
                )
            return status if status.get("state") == "COMPLETED" else None

        return self.wait(done, 90)

    def generation(self, name, revision):
        connector = self.api(
            "/_plugins/_ml/connectors/_create",
            "POST",
            {
                "name": "lifecycle-" + name,
                "description": "private deterministic fixture",
                "version": 1,
                "protocol": "http",
                "credential": {"openAI_key": "synthetic-fixture"},
                "parameters": {
                    "endpoint": f"provider-{revision.lower()}:8090",
                    "model": "lifecycle",
                },
                "actions": [
                    {
                        "action_type": "predict",
                        "method": "POST",
                        "url": "http://${parameters.endpoint}/v1/embeddings",
                        "headers": {
                            "Content-Type": "application/json",
                            "Authorization": "Bearer ${credential.openAI_key}",
                        },
                        "request_body": '{"input": ${parameters.input}, "model": "${parameters.model}"}',
                        "pre_process_function": "connector.pre_process.openai.embedding",
                        "post_process_function": "connector.post_process.openai.embedding",
                    }
                ],
            },
        )["connector_id"]
        registered = self.task(
            self.api(
                "/_plugins/_ml/models/_register",
                "POST",
                {
                    "name": "lifecycle-" + name,
                    "function_name": "remote",
                    "connector_id": connector,
                    "model_config": {
                        "model_type": "TEXT_EMBEDDING",
                        "embedding_dimension": 2,
                        "framework_type": "SENTENCE_TRANSFORMERS",
                        "additional_config": {"space_type": "cosinesimil"},
                    },
                },
            )
        )
        model = registered["model_id"]
        self.task(self.api("/_plugins/_ml/models/" + model + "/_deploy", "POST", {}))
        self.api(
            "/" + name,
            "PUT",
            {
                "settings": {
                    "index.knn": True,
                    "number_of_shards": 1,
                    "number_of_replicas": 0,
                },
                "mappings": {
                    "properties": {
                        "body": {
                            "type": "semantic",
                            "model_id": model,
                            "skip_existing_embedding": True,
                        },
                        "version": {"type": "integer"},
                        "tag": {"type": "keyword"},
                    }
                },
            },
        )
        self.revisions[name] = revision
        # A real application persists its authoritative source and serving
        # generation. The controller ledger is a fixture, not claimed product state.
        for identity, row in self.sources.items():
            self.api(f"/{name}/_doc/{identity}?refresh=true", "PUT", row)

    def write(self, identity, row, sync="full_index"):
        if sync == "write":
            raise ValueError("native inference-at-ingest is a derived-ready path")
        for name in self.revisions:
            self.api(f"/{name}/_doc/{identity}?refresh=true", "PUT", row)
        self.sources[identity] = dict(row)

    def metadata(self, identity, row):
        for name in self.revisions:
            # Partial scalar update avoids an artificial full-body re-embed.
            self.api(
                f"/{name}/_update/{identity}?refresh=true",
                "POST",
                {"doc": {"tag": row["tag"]}},
            )
        self.sources[identity]["tag"] = row["tag"]

    def delete(self, identity):
        for name in self.revisions:
            self.api(f"/{name}/_doc/{identity}?refresh=true", "DELETE")
        self.sources.pop(identity, None)

    def query(self, name):
        vector = [1, 0] if self.revisions[name] == "A" else [0, 1]
        result = self.api(
            f"/{name}/_search",
            "POST",
            {
                "size": 100,
                "query": {
                    "knn": {
                        "body_semantic_info.embedding": {"vector": vector, "k": 100}
                    }
                },
            },
        )
        if result.get("timed_out") or result.get("_shards", {}).get("failed", 0):
            raise RuntimeError("OpenSearch query failed or timed out")
        return {
            h["_id"]: h["_source"]["body_semantic_info"]["embedding"]
            for h in result.get("hits", {}).get("hits", [])
        }

    def complete(self, name):
        return self.api(f"/{name}/_count")["count"] == len(self.sources)

    def restart(self):
        self.docker(["kill", self.name])
        self.docker(["start", self.name])
        self.refresh()
        self.ready()
        self.api("/_cluster/health?wait_for_status=yellow&timeout=60s")
