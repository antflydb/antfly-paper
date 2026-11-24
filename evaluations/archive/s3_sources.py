"""Resolve an existing versioned S3 archive to immutable, credential-free source descriptors."""

import argparse
import json
from pathlib import Path


def main():
    import boto3

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inventory", type=Path, required=True)
    p.add_argument("--bucket", required=True)
    p.add_argument("--prefix", default="")
    p.add_argument("--endpoint", help="explicit S3-compatible storage endpoint")
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    client = boto3.client("s3", endpoint_url=args.endpoint)
    result = {}
    inventory = json.loads(args.inventory.read_text())
    for item in inventory["files"]:
        if not item["path"].endswith(".pdf"):
            continue
        name = Path(item["path"]).name
        key = "/".join(filter(None, [args.prefix.strip("/"), name]))
        head = client.head_object(Bucket=args.bucket, Key=key)
        version = head.get("VersionId")
        if not version or version == "null" or head["ContentLength"] != item["size"]:
            raise ValueError("expected immutable version and matching size for " + key)
        result[name] = {
            "bucket": args.bucket,
            "key": key,
            "version": version,
            "etag": head["ETag"],
        }
    with args.out.open("x") as f:
        json.dump(result, f, indent=2)


if __name__ == "__main__":
    main()
