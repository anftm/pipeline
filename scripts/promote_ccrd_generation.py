#!/usr/bin/env python3
"""Publish current.json for an already-built CCRD Bucket generation."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import batch_bucket_files, download_bucket_files

from publish_ccrd_index import (
    BUCKET,
    BUILDER_SHA256,
    BUILDER_URL,
    CURRENT_PATH,
    SOURCE_ARCHIVE_SHA256,
    SOURCE_ARCHIVE_URL,
    TOKENIZER_VERSION,
    inspect_database,
    sha256,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("generation")
    args = parser.parse_args()
    token = os.environ.get("HF_TOKEN", "")
    if not token:
        raise RuntimeError("HF_TOKEN is required")
    generation = args.generation.strip()
    if not generation.startswith("ccrd-") or "/" in generation:
        raise ValueError("invalid generation")

    workdir = Path(tempfile.mkdtemp(prefix="ccrd_promote_"))
    try:
        files = []
        for source in ("CCRD", "CW"):
            relative = f"generations/{generation}/{source}.sqlite3"
            local = workdir / f"{source}.sqlite3"
            files.append((source, relative, local))
        download_bucket_files(BUCKET, files=[(relative, local) for _source, relative, local in files], token=token, raise_on_missing_files=True)

        expected_counts = {"CCRD": 35586, "CW": 11664}
        databases = {}
        for source, relative, local in files:
            databases[source] = {
                "path": relative,
                "sha256": sha256(local),
                "bytes": local.stat().st_size,
                **inspect_database(local, expected_counts[source]),
            }
        manifest = {
            "format": 1,
            "source_archive": {"url": SOURCE_ARCHIVE_URL, "sha256": SOURCE_ARCHIVE_SHA256},
            "builder": {"url": BUILDER_URL, "sha256": BUILDER_SHA256},
            "tokenizer_version": TOKENIZER_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "databases": databases,
        }
        manifest_path = workdir / CURRENT_PATH
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        batch_bucket_files(BUCKET, delete=[CURRENT_PATH], token=token)
        batch_bucket_files(BUCKET, add=[(str(manifest_path), CURRENT_PATH)], token=token)
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
