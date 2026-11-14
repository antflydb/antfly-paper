"""Controller for a distinctly named custom durable PostgreSQL application."""

from lance import Lance


class Postgres(Lance):
    supports_async = True

    def __init__(self, owned, call, wait, docker, source, image):
        self.owned, self.call, self.wait, self.docker = owned, call, wait, docker
        owned.postgres(image=image, ulimits=["nofile=65536:524288"])
        self.name = owned.client(
            "pipeline",
            ["/lifecycle/postgres_service.py"],
            env=["PAPER_TOKEN", "PAPER_DB_URL"],
            mounts=[f"type=bind,src={source},dst=/lifecycle,readonly"],
            ports=[8091],
            cpus=1,
            memory="1g",
            ulimits=["nofile=65536:524288"],
        )
        self.refresh()
        self.ready()

    def write(self, identity, row, sync="full_index"):
        result = self.api("/write", {"id": identity, "row": row})
        if sync != "write":
            self.wait(lambda: all(self.complete(g) for g in self.revisions), 40)
        return result

    def generation(self, name, revision):
        result = self.api("/generation", {"name": name, "revision": revision})
        if not hasattr(self, "revisions"):
            self.revisions = {}
        self.revisions[name] = revision
        return result
