"""Native Vespa feed-time embedding and new-field reindex/cutover adapter."""

import io
import json
import urllib.error
import urllib.request
import zipfile

IMAGE = "vespaengine/vespa@sha256:e6d3943aef86fd3e57cceefc51d3005a3807f22a254383d348ae9563bbd68d6c"
VERSION = "8.781.11"


class Vespa:
    supports_async = False

    def __init__(self, owned, call, wait, docker):
        self.owned, self.call, self.wait, self.docker = owned, call, wait, docker
        self.revisions = {}
        self.expected = 0
        volume = owned.volume("vespa-data")
        self.name = owned.container(
            "vespa",
            IMAGE,
            [],
            mounts=[f"type=volume,src={volume},dst=/opt/vespa/var"],
            ports=[8080, 19071],
            ulimits=["nofile=65536:524288"],
        )
        self.url = f"http://127.0.0.1:{owned.port(self.name, 8080)}"
        self.admin = f"http://127.0.0.1:{owned.port(self.name, 19071)}"
        self.application = "/application/v2/tenant/default/application/default/environment/prod/region/default/instance/default"
        self.ready()
        self.deploy()

    def ready(self):
        def probe():
            try:
                return (
                    self.call(self.admin + "/state/v1/health")
                    .get("status", {})
                    .get("code")
                    == "up"
                )
            except (OSError, ValueError, RuntimeError):
                return False

        self.wait(probe, 180)

    def package(self):
        components = "".join(
            f'<component id="{g}" type="openai-embedder"><model>lifecycle</model><dimensions>2</dimensions><endpoint>http://provider-{r.lower()}:8090/v1/embeddings</endpoint></component>'
            for g, r in self.revisions.items()
        )
        fields = "".join(
            f"field {g} type tensor<float>(x[2]) {{ indexing: input body | embed {g} | attribute | summary\nattribute {{ distance-metric: angular }} }}"
            for g in self.revisions
        )
        ranking = "".join(
            f"rank-profile search_{g} {{ inputs {{ query(q) tensor<float>(x[2]) }} first-phase {{ expression: closeness(field,{g}) }} }}"
            for g in self.revisions
        )
        schema = (
            "schema lifecycle { document lifecycle { field doc_id type string { indexing: attribute | summary } field body type string { indexing: index | summary } field version type int { indexing: attribute | summary } field tag type string { indexing: summary } } "
            + fields
            + " "
            + ranking
            + " }"
        )
        services = f'<services version="1.0"><container id="default" version="1.0"><search/><document-api/>{components}<nodes><node hostalias="node1"/></nodes></container><content id="content" version="1.0"><redundancy>1</redundancy><documents><document type="lifecycle" mode="index"/></documents><nodes><node hostalias="node1" distribution-key="0"/></nodes></content></services>'
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("services.xml", services)
            archive.writestr(
                "hosts.xml",
                '<hosts><host name="localhost"><alias>node1</alias></host></hosts>',
            )
            archive.writestr("schemas/lifecycle.sd", schema)
        return buffer.getvalue()

    def deploy(self):
        req = urllib.request.Request(
            self.admin + "/application/v2/tenant/default/prepareandactivate",
            data=self.package(),
            headers={"Content-Type": "application/zip"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"Vespa deployment {exc.code}: {exc.read(1200).decode(errors='replace')}"
            ) from exc
        if "session-id" not in result:
            raise RuntimeError("Vespa deployment did not return a session")

        def converged():
            try:
                return self.call(
                    self.admin + self.application + "/serviceconverge"
                ).get("converged", False)
            except (OSError, RuntimeError):
                return False

        self.wait(converged, 180)

    def generation(self, name, revision):
        self.revisions[name] = revision
        self.deploy()
        self.call(
            self.admin
            + self.application
            + "/reindex?clusterId=content&documentType=lifecycle",
            "POST",
            {},
        )
        self.deploy()

    def write(self, identity, row, sync="full_index"):
        if sync == "write":
            raise ValueError(
                "native Vespa feed is a derived-ready path; no async-source acknowledgement claimed"
            )
        fields = {"doc_id": identity, **row}
        self.call(
            self.url + "/document/v1/lifecycle/lifecycle/docid/" + identity,
            "POST",
            {"fields": fields},
            40,
        )

    def metadata(self, identity, row):
        self.call(
            self.url + "/document/v1/lifecycle/lifecycle/docid/" + identity,
            "PUT",
            {"fields": {"tag": {"assign": row["tag"]}}},
            40,
        )

    def delete(self, identity):
        self.call(
            self.url + "/document/v1/lifecycle/lifecycle/docid/" + identity, "DELETE"
        )

    def query(self, name):
        rev = self.revisions[name]
        result = self.call(
            self.url + "/search/",
            "POST",
            {
                "yql": f"select * from lifecycle where ({{targetHits:100,approximate:false}}nearestNeighbor({name},q))",
                "input.query(q)": [1, 0] if rev == "A" else [0, 1],
                "ranking": "search_" + name,
                "hits": 100,
                "timeout": "10s",
            },
        )
        if result.get("root", {}).get("errors"):
            raise RuntimeError("Vespa query returned errors")
        rows = {}
        for hit in result.get("root", {}).get("children", []):
            fields = hit.get("fields", {})
            if "doc_id" not in fields or name not in fields:
                continue
            vector = fields[name]
            if isinstance(vector, dict):
                vector = vector.get("values") or [
                    x["value"] for x in vector.get("cells", [])
                ]
            rows[fields["doc_id"]] = vector
        return rows

    def complete(self, name):
        result = self.call(self.admin + self.application + "/reindexing")
        states = []
        for cluster in result.get("clusters", {}).values():
            if isinstance(cluster, dict):
                docs = cluster.get("ready", {})
                if "lifecycle" in docs:
                    states.append(docs["lifecycle"].get("state"))
        return bool(states) and all(s == "successful" for s in states)

    def restart(self):
        self.docker(["kill", self.name])
        self.docker(["start", self.name])
        self.url = f"http://127.0.0.1:{self.owned.port(self.name, 8080)}"
        self.admin = f"http://127.0.0.1:{self.owned.port(self.name, 19071)}"
        self.ready()
        # Container/config readiness and serving readiness are distinct.
        self.wait(lambda: self._serving(), 180)

    def _serving(self):
        try:
            return (
                self.call(self.url + "/state/v1/health").get("status", {}).get("code")
                == "up"
            )
        except (OSError, RuntimeError):
            return False
