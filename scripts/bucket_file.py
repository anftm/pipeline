#!/usr/bin/env python3
"""Read metadata for one exact Hugging Face bucket file; never delete it."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys


_BUCKET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def validate_target(bucket: str, path: str) -> None:
    if not _BUCKET.fullmatch(bucket):
        raise ValueError("bucket must be an exact namespace/name")
    if (not path or path.startswith("/") or "\\" in path
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or any(part in {"", ".", ".."} for part in path.split("/"))):
        raise ValueError("path must be an exact file path, not a directory or URL")


def file_info(api, bucket: str, path: str) -> dict | None:
    """Query only the specified file, never a tree or reference graph."""
    items = list(api.get_bucket_paths_info(bucket, [path]))
    if not items:
        return None
    if len(items) != 1 or items[0].type != "file" or items[0].path != path:
        raise RuntimeError("bucket API returned an unexpected file identity")
    item = items[0]
    mtime = getattr(item, "mtime", None)
    return {"bucket": bucket, "path": path, "size": item.size,
            "xet_hash": item.xet_hash, "mtime": mtime.isoformat() if mtime else None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bucket", help="bucket namespace/name")
    parser.add_argument("path", help="exact file path; wildcards are not expanded")
    parser.add_argument("--token-env", default="HF_TOKEN", help="environment variable containing HF token")
    args = parser.parse_args(argv)
    try:
        validate_target(args.bucket, args.path)
        token = os.environ.get(args.token_env)
        if args.token_env != "HF_TOKEN" and not token:
            raise ValueError(f"token environment variable is not set: {args.token_env}")
        from huggingface_hub import HfApi
        info = file_info(HfApi(token=token), args.bucket, args.path)
        if info is None:
            raise FileNotFoundError(f"file not found: {args.bucket}/{args.path}")
        print(json.dumps({"status": "found", **info}, ensure_ascii=False))
        return 0
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
