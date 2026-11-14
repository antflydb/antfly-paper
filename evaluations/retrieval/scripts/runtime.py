"""Campaign-owned Linux resources; no attachment to pre-existing services."""

import hashlib
import json
import os
import secrets
import subprocess
import time
import urllib.request
import uuid
from functools import lru_cache
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[3]
ROOT = Path(os.environ.get("PAPER_WORKSPACE", str(REPOSITORY))).resolve()
CAMPAIGN = REPOSITORY / "evaluations/retrieval"
CLIENT_IMAGE = os.environ.get("PAPER_CLIENT_IMAGE", "antfly-paper-eval:0.3.0")
POSTGRES_IMAGE = "paradedb/paradedb@sha256:4df3bcf9f19b32821446967f7874d1a40bee8b9550184cc0d70e93f51ee8af36"
PGVECTOR_IMAGE = "pgvector/pgvector@sha256:ab49524d18df7200b74ac9a475e1db6297fc1eb57da4c214c63d10ecc4b754fa"
LEGACY_ANTFLY_RUNTIME = "pgvector/pgvector@sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b"
SUBJECTS = {
    "v0.3.0-rc.17": "9096dab1fcd4bbbfed99bc6d0f00ec0f79a53c376bd4d48676e8fb2e62946075",
    "v0.3.0": "6b39c2a425fc946779d69572831c891fb20303efe8517d1b3b79a6b4f0f8c998",
}
SUBJECT_TAG = os.environ.get("PAPER_SUBJECT_TAG", "v0.3.0")
ANTFLY_PROFILE = os.environ.get("PAPER_ANTFLY_PROFILE", "default")
if SUBJECT_TAG not in SUBJECTS or ANTFLY_PROFILE not in {"default", "tuned"}:
    raise ValueError("unknown paper subject or Antfly profile")
ANTFLY = ROOT / f"bin/{SUBJECT_TAG}/Linux_arm64/antfly"
ANTFLY_SHA = SUBJECTS[SUBJECT_TAG]
ANTFLY_IMAGE = "ghcr.io/antflydb/antfly@sha256:115a05eff4729b8a72660a9a682a8f9e6122358d8d0f0c85042baafd9fc59294"
ANTFLY_RUNTIME = ANTFLY_IMAGE if SUBJECT_TAG == "v0.3.0" else LEGACY_ANTFLY_RUNTIME
LABEL = "antfly-paper.retrieval=paper-default-v030-v4"


def docker(args, *, env=None, input=None, timeout=120):
    return subprocess.check_output(
        ["docker", *args],
        text=True,
        input=input,
        env=env,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )


def request(url, body=None, token=None, timeout=60):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None if body is None else json.dumps(body).encode()
    with urllib.request.urlopen(
        urllib.request.Request(url, data=data, headers=headers), timeout=timeout
    ) as response:
        return json.load(response)


def wait_for(predicate, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise TimeoutError("bounded readiness deadline exceeded")


@lru_cache(maxsize=1)
def get_client_image():
    return docker(["image", "inspect", CLIENT_IMAGE, "--format", "{{.Id}}"]).strip()


class Owned:
    def __init__(self, output):
        self.output = Path(output)
        self.prefix = "paper-cmp-" + uuid.uuid4().hex[:10]
        self.network = self.prefix
        self.containers, self.volumes = [], []
        self.password = secrets.token_hex(24)
        self.token = secrets.token_hex(24)
        self.environment = {
            **os.environ,
            "POSTGRES_PASSWORD": self.password,
            "PGPASSWORD": self.password,
            "PAPER_PASSWORD": self.password,
            "PAPER_TOKEN": self.token,
            "PAPER_SUBJECT_TAG": SUBJECT_TAG,
            "PAPER_ANTFLY_PROFILE": ANTFLY_PROFILE,
            "PAPER_EXACT_CONTROL_QUERIES": os.environ.get(
                "PAPER_EXACT_CONTROL_QUERIES", "5"
            ),
            "PAPER_PROTOCOL": os.environ.get("PAPER_PROTOCOL", "paper-default-v030-v4"),
        }

    def start(self):
        self.output.mkdir(parents=True, exist_ok=False)
        docker(["network", "create", "--internal", "--label", LABEL, self.network])

    def volume(self, role):
        name = self.prefix + "-" + role
        docker(["volume", "create", "--label", LABEL, name])
        self.volumes.append(name)
        return name

    def container(
        self,
        role,
        image,
        args,
        *,
        env=(),
        mounts=(),
        ports=(),
        cpus=4,
        memory="12g",
        entrypoint=None,
        ulimits=(),
    ):
        name = self.prefix + "-" + role
        command = [
            "run",
            "-d",
            "--name",
            name,
            "--label",
            LABEL,
            "--platform",
            "linux/arm64",
            "--network",
            self.network,
            "--network-alias",
            role,
            "--cpus",
            str(cpus),
            "--memory",
            memory,
            "--shm-size",
            "8g",
        ]
        for key in env:
            command += ["-e", key]
        for mount in mounts:
            command += ["--mount", mount]
        for port in ports:
            command += ["-p", f"127.0.0.1::{port}"]
        for limit in ulimits:
            command += ["--ulimit", limit]
        if entrypoint:
            command += ["--entrypoint", entrypoint]
        command += [image, *args]
        docker(command, env=self.environment, timeout=300)
        self.containers.append(name)
        return name

    def port(self, name, port):
        return int(docker(["port", name, f"{port}/tcp"]).strip().rsplit(":", 1)[1])

    def postgres(self, image=POSTGRES_IMAGE, *, ulimits=()):
        volume = self.volume("postgres-data")
        name = self.container(
            "postgres",
            image,
            [
                "postgres",
                "-c",
                "shared_buffers=1GB",
                "-c",
                "maintenance_work_mem=6GB",
                "-c",
                "max_parallel_maintenance_workers=2",
            ],
            env=["POSTGRES_PASSWORD"],
            mounts=[f"type=volume,src={volume},dst=/var/lib/postgresql"],
            ports=[5432],
            ulimits=ulimits,
        )

        def ready():
            try:
                return self.sql(name, "SELECT 1;").strip() == "1"
            except subprocess.CalledProcessError:
                return False

        wait_for(ready, 60)
        self.environment["PAPER_DB_URL"] = (
            f"postgresql://postgres:{self.password}@postgres:5432/postgres"
        )
        return name

    def sql(self, name, sql):
        return docker(
            [
                "exec",
                "-i",
                "-e",
                "PGPASSWORD",
                name,
                "psql",
                "-h",
                "127.0.0.1",
                "-U",
                "postgres",
                "-d",
                "postgres",
                "-qAt",
                "-v",
                "ON_ERROR_STOP=1",
            ],
            env=self.environment,
            input=sql,
        )

    def client(
        self,
        role,
        args,
        *,
        env=(),
        mounts=(),
        ports=(),
        cpus=1,
        memory="1g",
        ulimits=(),
    ):
        return self.container(
            role,
            get_client_image(),
            args,
            env=["PAPER_PASSWORD", "PAPER_TOKEN", *env],
            mounts=[
                f"type=bind,src={CAMPAIGN / 'scripts'},dst=/driver,readonly",
                *mounts,
            ],
            ports=ports,
            cpus=cpus,
            memory=memory,
            ulimits=ulimits,
        )

    def wait_exit(self, name, timeout=120):
        return int(docker(["wait", name], timeout=timeout).strip())

    def logs(self, name):
        value = docker(["logs", "--tail", "100", name])
        # Never retain ephemeral database credentials or signed URLs.
        for secret in (
            self.password,
            self.token,
            self.environment.get("PAPER_DB_URL", ""),
        ):
            if secret:
                value = value.replace(secret, "[redacted]")
        return value

    def close(self, success=False):
        for name in reversed(self.containers):
            try:
                (self.output / (name.rsplit("-", 1)[-1] + ".log")).write_text(
                    self.logs(name)
                )
                docker(["stop", "--time", "15", name], timeout=30)
                if success:
                    docker(["rm", name])
            except subprocess.CalledProcessError:
                pass
        if success:
            for volume in self.volumes:
                docker(["volume", "rm", volume])
            docker(["network", "rm", self.network])
        else:
            (self.output / "diagnosis-state.json").write_text(
                json.dumps(
                    {
                        "network": self.network,
                        "containers": self.containers,
                        "volumes": self.volumes,
                    },
                    indent=2,
                )
                + "\n"
            )


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_records(output, result, provenance):
    output = Path(output)
    for name, value in (("result.json", result), ("provenance.json", provenance)):
        payload = json.dumps(value, indent=2) + "\n"
        if len(payload.encode()) >= 1048576:
            raise ValueError("retained record exceeds budget")
        with (output / name).open("x") as stream:
            stream.write(payload)
    (output / "receipt.json").write_text(
        json.dumps(
            {
                "sha256": {
                    name: sha(output / name)
                    for name in ("result.json", "provenance.json")
                }
            },
            indent=2,
        )
        + "\n"
    )
