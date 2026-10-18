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
    read_current,
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
        previous = read_current(workdir, token)
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
        saved_manifest = workdir / 'generation-manifest.json'
        download_bucket_files(BUCKET, files=[(f"generations/{generation}/manifest.json", str(saved_manifest))],
                              token=token, raise_on_missing_files=True)
        manifest = json.loads(saved_manifest.read_text(encoding='utf-8'))
        if (manifest.get('format') != 1 or manifest.get('tokenizer_version') != TOKENIZER_VERSION
                or manifest.get('databases') != databases):
            raise ValueError('stored generation manifest does not match verified databases')
        manifest_path = workdir / CURRENT_PATH
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        if read_current(workdir, token) != previous:
            raise RuntimeError('CCRD pointer changed before promotion')
        batch_bucket_files(BUCKET, add=[(str(manifest_path), CURRENT_PATH)], token=token)
        if read_current(workdir, token) != manifest:
            raise RuntimeError('CCRD promotion readback mismatch')
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
