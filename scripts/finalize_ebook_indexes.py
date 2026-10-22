#!/usr/bin/env python3
"""Merge ebook shard indexes into one canonical index per extension."""

from __future__ import annotations

import json
import os
import argparse
import re

import boto3
from botocore.config import Config

try:
    from .reader_stream_index import merge_shards, validate_index
    from .reader_bucket_store import retry_s3
except ImportError:
    from reader_stream_index import merge_shards, validate_index
    from reader_bucket_store import retry_s3


EXTENSIONS = ("epub", "mobi", "azw3", "fb2", "chm")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--extension", choices=EXTENSIONS, action="append")
    args = parser.parse_args()
    namespace, bucket = os.environ.get("HF_S3_BUCKET", "vomebook/reader-assets-v2").rsplit("/", 1)
    client = boto3.client(
        "s3", endpoint_url=f"https://s3.hf.co/{namespace}",
        aws_access_key_id=os.environ["HF_S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["HF_S3_SECRET_ACCESS_KEY"],
        config=Config(region_name="us-east-1", s3={"addressing_style": "path"},
                      retries={"mode": "adaptive", "max_attempts": 8}),
    )
    for extension in args.extension or EXTENSIONS:
        root = f"chapters/ebook/{extension}"
        keys = []
        for page in client.get_paginator("list_objects_v2").paginate(
                Bucket=bucket, Prefix=f"{root}/index-"):
            keys.extend(item["Key"] for item in page.get("Contents", [])
                        if re.fullmatch(re.escape(root) + r"/index-0[0-7]\.json", item["Key"]))
        if not keys:
            continue
        def read(key):
            def load():
                response = client.get_object(Bucket=bucket, Key=key)
                try:
                    return validate_index(json.loads(response["Body"].read()), key)
                finally:
                    response["Body"].close()
            try:
                return retry_s3(load)
            except FileNotFoundError:
                if key != f"{root}/index.json":
                    raise
                return {"files": [], "failures": []}

        canonical = read(f"{root}/index.json")
        shards = []
        for key in sorted(keys):
            shards.append((int(key.rsplit("-", 1)[1].split(".")[0]), read(key)))
        merged = merge_shards(canonical, shards, "ebook-chapter-stream-index", {extension})
        body = (json.dumps(merged, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
        retry_s3(lambda: client.put_object(Bucket=bucket, Key=f"{root}/index.json", Body=body,
                                          ContentType="application/json"))
        if read(f"{root}/index.json") != merged:
            raise ValueError(f"canonical ebook index readback failed: {root}")
        print(f"{extension}: merged {len(merged['files'])} files, {len(merged['failures'])} failures from {len(keys)} shard indexes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
