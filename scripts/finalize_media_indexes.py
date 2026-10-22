#!/usr/bin/env python3
"""Merge media shard indexes into canonical audio/video/SWF indexes."""

from __future__ import annotations

import json
import os
import argparse
import re

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

try:
    from .reader_stream_index import merge_shards, validate_index
    from .pilot_media_stream import MEDIA_EXTENSIONS
    from .reader_assets import bucket_conversion_contract
except ImportError:
    from reader_stream_index import merge_shards, validate_index
    from pilot_media_stream import MEDIA_EXTENSIONS
    from reader_assets import bucket_conversion_contract


KINDS = ("audio", "video", "swf")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=KINDS, action="append")
    args = parser.parse_args()
    namespace, bucket = os.environ.get("HF_S3_BUCKET", "vomebook/reader-assets-v2").rsplit("/", 1)
    client = boto3.client(
        "s3", endpoint_url=f"https://s3.hf.co/{namespace}",
        aws_access_key_id=os.environ["HF_S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["HF_S3_SECRET_ACCESS_KEY"],
        config=Config(region_name="us-east-1", s3={"addressing_style": "path"},
                      retries={"mode": "adaptive", "max_attempts": 8}),
    )
    for kind in args.kind or KINDS:
        root = f"media/{kind}"
        keys = []
        for page in client.get_paginator("list_objects_v2").paginate(
                Bucket=bucket, Prefix=f"{root}/index-"):
            keys.extend(item["Key"] for item in page.get("Contents", [])
                        if re.fullmatch(re.escape(root) + r"/index-0[0-7]\.json", item["Key"]))
        if not keys:
            continue
        def read(key):
            try:
                response = client.get_object(Bucket=bucket, Key=key)
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                    return {"files": [], "failures": []}
                raise
            try:
                return validate_index(json.loads(response["Body"].read()), key)
            finally:
                response["Body"].close()

        canonical = read(f"{root}/index.json")
        shards = []
        for key in sorted(keys):
            shards.append((int(key.rsplit("-", 1)[1].split(".")[0]), read(key)))
        extensions = {ext for ext in MEDIA_EXTENSIONS
                      if (bucket_conversion_contract("", "", ext) or (None, None))[1] == kind}
        merged = merge_shards(canonical, shards, "media-stream-index", extensions)
        body = (json.dumps(merged, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
        client.put_object(Bucket=bucket, Key=f"{root}/index.json", Body=body,
                          ContentType="application/json")
        print(f"{kind}: merged {len(merged['files'])} files, {len(merged['failures'])} failures from {len(keys)} shard indexes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
