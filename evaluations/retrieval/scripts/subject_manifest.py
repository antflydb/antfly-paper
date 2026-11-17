"""Explicit immutable candidate subjects, separate from released cohorts."""

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path


def load_subject(path, protocol, requested_tag=None):
    if not path:
        return None
    raw = Path(path).read_bytes()
    value = json.loads(raw)
    required = {"name", "commit", "image", "binary_sha256", "build_profile"}
    if set(value) != required:
        raise ValueError(
            "candidate subject requires exactly " + ", ".join(sorted(required))
        )
    if not re.fullmatch(r"main-[0-9a-f]{12}", value["name"]):
        raise ValueError("candidate name must identify a pinned main commit")
    if (
        not re.fullmatch(r"[0-9a-f]{40}", value["commit"])
        or value["name"] != "main-" + value["commit"][:12]
    ):
        raise ValueError("candidate commit/name mismatch")
    if not re.fullmatch(r"(?:[a-zA-Z0-9./:_-]+@)?sha256:[0-9a-f]{64}", value["image"]):
        raise ValueError("candidate image must be immutable")
    if not re.fullmatch(r"[0-9a-f]{64}", value["binary_sha256"]):
        raise ValueError("candidate binary checksum missing")
    if not value["build_profile"] or not isinstance(value["build_profile"], str):
        raise ValueError("candidate build profile missing")
    if not protocol.startswith("paper-main-") or (
        requested_tag and requested_tag != value["name"]
    ):
        raise ValueError(
            "candidate needs a distinct main protocol and matching subject name"
        )
    return {**value, "manifest_sha256": hashlib.sha256(raw).hexdigest()}


@lru_cache(maxsize=None)
def verify_image(image, commit, binary_sha256, docker, allow_unlabeled_release=False):
    value = json.loads(docker(["image", "inspect", image]))[0]
    revision = value.get("Config", {}).get("Labels", {}).get("org.opencontainers.image.revision")
    # Official release images can omit the optional OCI revision label. Their
    # caller binds the source through the publisher's checksum-verified release
    # archive. This exception never admits a contradictory label, another
    # registry, mutable references, or different executable bytes.
    unlabeled_release = (
        revision is None
        and allow_unlabeled_release
        and re.fullmatch(r"ghcr\.io/antflydb/antfly@sha256:[0-9a-f]{64}", image)
    )
    if revision != commit and not unlabeled_release:
        raise ValueError("candidate image source label does not match pinned commit")
    actual = docker(
        [
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "sha256sum",
            image,
            "/antfly",
        ]
    ).split()[0]
    if actual != binary_sha256:
        raise ValueError("candidate image executable checksum mismatch")
    return value["Id"]
