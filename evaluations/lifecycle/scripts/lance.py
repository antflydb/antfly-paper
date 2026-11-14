"""Controller for the native synchronous LanceDB SDK application."""


class Lance:
    supports_async = False

    def __init__(self, owned, call, wait, docker, source):
        self.owned, self.call, self.wait, self.docker = owned, call, wait, docker
        volume = owned.volume("lance-data")
        self.name = owned.client(
            "lance",
            ["/lifecycle/lance_service.py"],
            env=["PAPER_TOKEN"],
            mounts=[
                f"type=bind,src={source},dst=/lifecycle,readonly",
                f"type=volume,src={volume},dst=/lance",
            ],
            ports=[8091],
            cpus=4,
            memory="12g",
            ulimits=["nofile=65536:524288"],
        )
        self.refresh()
        self.ready()

    def refresh(self):
        self.url = f"http://127.0.0.1:{self.owned.port(self.name, 8091)}"

    def api(self, path, body=None):
        return self.call(
            self.url + path, "POST", body or {}, timeout=90, token=self.owned.token
        )

    def ready(self):
        def probe():
            try:
                return self.api("/ready").get("ready", False)
            except (OSError, RuntimeError):
                return False

        self.wait(probe, 90)

    def generation(self, name, revision):
        return self.api("/generation", {"name": name, "revision": revision})

    def write(self, identity, row, sync="full_index"):
        if sync == "write":
            raise ValueError(
                "synchronous SDK application has no separate source acknowledgement"
            )
        return self.api("/write", {"id": identity, "row": row})

    def metadata(self, identity, row):
        return self.api("/metadata", {"id": identity, "row": row})

    def delete(self, identity):
        return self.api("/delete", {"id": identity})

    def query(self, name):
        return self.api("/query", {"name": name})

    def complete(self, name):
        return self.api("/complete", {"name": name})["complete"]

    def restart(self):
        self.docker(["kill", self.name])
        self.docker(["start", self.name])
        self.refresh()
        self.ready()
