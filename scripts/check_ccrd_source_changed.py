#!/usr/bin/env python3
"""Check whether the changing CCRD source requires a new index generation."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from huggingface_hub import download_bucket_files

from ccrd_source_override import CORPUS_MIGRATION_VERSION, CORPUS_PATH, download_override
from publish_ccrd_index import (
    BUCKET, BUILDER_SHA256, CURRENT_PATH, SOURCE_ARCHIVE_SHA256,
)


def write_output(changed: bool) -> None:
    output = os.environ.get("GITHUB_OUTPUT", "")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"changed={'true' if changed else 'false'}\n")
    print(f"CCRD source changed: {changed}")


def main() -> int:
    token = os.environ.get("HF_TOKEN", "")
    if not token:
        raise RuntimeError("HF_TOKEN is required")

    workdir = Path(tempfile.mkdtemp(prefix="ccrd-source-check-"))
    try:
        current_path = workdir / CURRENT_PATH
        try:
            download_bucket_files(
                BUCKET,
                files=[(CURRENT_PATH, str(current_path))],
                token=token,
                raise_on_missing_files=True,
            )
            manifest = json.loads(current_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"No usable current manifest ({type(exc).__name__}); rebuilding")
            write_output(True)
            return 0

        # A builder or corpus change invalidates the published index even when the
        # single changing source text is unchanged. Without these checks a fixed
        # index builder would never be applied to an existing generation.
        published_builder = (manifest.get("builder") or {}).get("sha256")
        if published_builder != BUILDER_SHA256:
            print(f"Builder hash changed: {published_builder or '(missing)'} -> {BUILDER_SHA256}")
            write_output(True)
            return 0
        published_archive = (manifest.get("source_archive") or {}).get("sha256")
        if published_archive != SOURCE_ARCHIVE_SHA256:
            print(f"Corpus archive changed: {published_archive or '(missing)'} -> {SOURCE_ARCHIVE_SHA256}")
            write_output(True)
            return 0
        if manifest.get("corpus_migration") != CORPUS_MIGRATION_VERSION:
            print(f"Corpus layout migration changed: {manifest.get('corpus_migration') or '(missing)'} -> {CORPUS_MIGRATION_VERSION}")
            write_output(True)
            return 0

        source_path = workdir / "source.txt"
        details = download_override(source_path)
        recorded = (
            manifest.get("source_overrides", {})
            .get(str(CORPUS_PATH), {})
            .get("sha256")
        )
        changed = recorded != details["sha256"]
        print(f"Recorded source hash: {recorded or '(missing)'}")
        print(f"Current source hash:  {details['sha256']}")
        write_output(changed)
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
