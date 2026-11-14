"""Fetch the pinned public release; reject unexpected paths and checksums."""

import argparse
import hashlib
import json
import tarfile
import urllib.request
from pathlib import Path, PurePosixPath

root = Path(__file__).resolve().parents[3]
parser = argparse.ArgumentParser()
parser.add_argument("--tag", default="v0.3.0")
args = parser.parse_args()
subject = json.loads((root / "evaluations/retrieval/subjects.json").read_text())[
    args.tag
]
output = root / "bin" / args.tag / subject["platform"]
output.mkdir(parents=True, exist_ok=False)
archive = output / "release.tar.gz"
digest = hashlib.sha256()
with (
    urllib.request.urlopen(subject["archive_url"], timeout=60) as response,
    archive.open("xb") as stream,
):
    for block in iter(lambda: response.read(1024 * 1024), b""):
        stream.write(block)
        digest.update(block)
if digest.hexdigest() != subject["archive_sha256"]:
    raise ValueError("release archive checksum mismatch")
with tarfile.open(archive) as bundle:
    for member in bundle.getmembers():
        name = PurePosixPath(member.name)
        if name.is_absolute() or ".." in name.parts or member.isdev():
            raise ValueError("unsafe archive member")
        if member.issym() or member.islnk():
            target = PurePosixPath(member.linkname)
            if target.is_absolute() or ".." in target.parts:
                raise ValueError("unsafe archive link")
    bundle.extractall(
        output, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {})
    )
digest = hashlib.sha256()
with (output / "antfly").open("rb") as stream:
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(block)
if digest.hexdigest() != subject["binary_sha256"]:
    raise ValueError("release binary checksum mismatch")
(output / "provenance.json").write_text(json.dumps(subject, indent=2) + "\n")
print("Verified", args.tag, subject["platform"], subject["binary_sha256"])
