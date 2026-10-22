#!/usr/bin/env python3
"""Delete v2 image shard indexes before a full image rebuild.

Only ``pages/image/index-v2*.json`` is touched. The old
``pages/image/index.json`` and its objects are intentionally retained until the
new sidecar is published and verified.
"""

from __future__ import annotations

import argparse
import os

from huggingface_hub import HfApi


BUCKET = "vomebook/reader-assets-v2"
PREFIX = "pages/image/index-v2"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    token = os.environ.get("HF_TOKEN")
    api = HfApi(token=token)
    paths = sorted(
        item.path for item in api.list_bucket_tree(BUCKET, prefix=PREFIX, recursive=True, token=token)
        if item.type == "file"
        and item.path.rsplit("/", 1)[-1].startswith("index-v2")
        and item.path.endswith(".json")
    )
    print(f"v2 image indexes={len(paths)}")
    for path in paths:
        print(path)
    if args.apply and paths:
        api.batch_bucket_files(BUCKET, delete=paths, token=token)
        print(f"deleted {len(paths)} v2 image index file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
