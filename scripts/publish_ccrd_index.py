#!/usr/bin/env python3
"""Build CCRD full-text indexes from the deployed Space and publish the latest generation."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import batch_bucket_files, download_bucket_files, list_bucket_tree

from ccrd_source_override import CORPUS_PATH, download_override


SOURCE_ARCHIVE_URL = "https://github.com/anftm/pipeline/releases/download/ccrd/ccrd-corpus.tar.gz"
SOURCE_ARCHIVE_SHA256 = "a6b1615054303740e892e535e3c9a6d4f4805ac572f7f1577b10685249a862f1"
BUILDER_URL = "https://huggingface.co/spaces/vomebook/Search/resolve/main/build_fulltext_db.py"
BUILDER_SHA256 = "293f0c65db6a4709cb756f9bbbcaa95d3f7dbb2442e21e91f9f181f166dc1e53"
BUCKET = "vomebook/ccrd-index"
SOURCES = ("CCRD", "CW")
TOKENIZER_VERSION = "cjk-bigram-boundary-fts5-v6-snippet-anchors"
CURRENT_PATH = "current.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "VoiceOfML-CCRD-Index-Pipeline/1.0"})
    with urllib.request.urlopen(request, timeout=300) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)


def extract_corpus(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isdir() or member.isfile()):
                raise RuntimeError(f"unsafe archive member: {member.name}")
        bundle.extractall(destination, filter="data")


def expected_counts(search_data: Path) -> dict[str, int]:
    payload = json.loads(gzip.decompress(search_data.read_bytes()).decode("utf-8"))
    counts = {source: 0 for source in SOURCES}
    for record in payload.get("records", []):
        source = record.get("source")
        if source in counts:
            counts[source] += 1
    if not all(counts.values()):
        raise RuntimeError(f"missing CCRD or CW records in search data: {counts}")
    return counts


def inspect_database(path: Path, expected_documents: int) -> dict[str, int | str]:
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro&immutable=1", uri=True) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        tokenizer = connection.execute(
            "SELECT value FROM metadata WHERE key = 'tokenizer_version'"
        ).fetchone()
        documents = int(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
        fts_rows = int(connection.execute("SELECT COUNT(*) FROM content_fts").fetchone()[0])
        postings = int(connection.execute("SELECT COUNT(*) FROM postings").fetchone()[0])
        anchors = int(connection.execute("SELECT COUNT(*) FROM snippet_anchors").fetchone()[0])
    if integrity != "ok":
        raise RuntimeError(f"{path.name}: integrity_check returned {integrity!r}")
    if not tokenizer or tokenizer[0] != TOKENIZER_VERSION:
        raise RuntimeError(f"{path.name}: unexpected tokenizer version {tokenizer!r}")
    if documents != expected_documents or fts_rows != expected_documents:
        raise RuntimeError(
            f"{path.name}: expected {expected_documents} documents/FTS rows, got {documents}/{fts_rows}"
        )
    if anchors <= 0:
        raise RuntimeError(f"{path.name}: snippet anchors are missing")
    return {"documents": documents, "fts_rows": fts_rows, "postings": postings, "snippet_anchors": anchors}


def read_current(workdir: Path, token: str) -> dict | None:
    target = workdir / CURRENT_PATH
    try:
        download_bucket_files(BUCKET, files=[(CURRENT_PATH, str(target))], token=token)
    except Exception as exc:
        print(f"No prior current manifest: {type(exc).__name__}", flush=True)
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("existing current.json is unreadable; refusing to replace it") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-workdir", action="store_true")
    args = parser.parse_args()
    token = os.environ.get("HF_TOKEN", "")
    if not token:
        raise RuntimeError("HF_TOKEN is required")

    workdir = Path(tempfile.mkdtemp(prefix="ccrd_index_"))
    try:
        archive = workdir / "ccrd-corpus.tar.gz"
        print("Downloading CCRD corpus release asset", flush=True)
        download(SOURCE_ARCHIVE_URL, archive)
        archive_digest = sha256(archive)
        if archive_digest != SOURCE_ARCHIVE_SHA256:
            raise RuntimeError(f"unexpected corpus archive SHA-256: {archive_digest}")

        checkout = workdir / "source"
        checkout.mkdir()
        extract_corpus(archive, checkout)
        builder = checkout / "build_fulltext_db.py"
        print("Downloading current CCRD index builder", flush=True)
        download(BUILDER_URL, builder)
        builder_digest = sha256(builder)
        if builder_digest != BUILDER_SHA256:
            raise RuntimeError(f"unexpected builder SHA-256: {builder_digest}")
        search_data = checkout / "data/search_data.json.gz"
        if not search_data.exists():
            raise RuntimeError("source checkout lacks data/search_data.json.gz")
        counts = expected_counts(search_data)
        corpus_target = checkout / CORPUS_PATH
        if not corpus_target.is_file():
            raise RuntimeError(f"corpus lacks legacy CCRD target path: {CORPUS_PATH}")
        print("Replacing the changing CCRD source text", flush=True)
        override = download_override(corpus_target)

        print("Building CCRD full-text indexes", flush=True)
        subprocess.run([sys.executable, "build_fulltext_db.py"], cwd=checkout, check=True)

        files: dict[str, Path] = {}
        databases: dict[str, dict[str, int | str]] = {}
        for source in SOURCES:
            path = checkout / "data/fulltext" / f"{source}.sqlite3"
            if not path.exists():
                raise RuntimeError(f"builder did not create {path}")
            databases[source] = inspect_database(path, counts[source])
            files[source] = path

        generation = f"ccrd-{archive_digest[:16]}-{builder_digest[:16]}-{override['sha256'][:16]}"
        prefix = f"generations/{generation}"
        manifest = {
            "format": 1,
            "source_archive": {
                "url": SOURCE_ARCHIVE_URL,
                "sha256": archive_digest,
                "bytes": archive.stat().st_size,
            },
            "builder": {"url": BUILDER_URL, "sha256": builder_digest},
            "source_overrides": {str(CORPUS_PATH): override},
            "tokenizer_version": TOKENIZER_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "databases": {
                source: {
                    "path": f"{prefix}/{source}.sqlite3",
                    "sha256": sha256(path),
                    "bytes": path.stat().st_size,
                    **databases[source],
                }
                for source, path in files.items()
            },
        }
        manifest_path = workdir / "current.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")

        read_current(workdir, token)
        print(f"Uploading completed generation {generation}", flush=True)
        batch_bucket_files(
            BUCKET,
            add=[(str(path), f"{prefix}/{source}.sqlite3") for source, path in files.items()],
            token=token,
        )
        # Publish the pointer only after both immutable database files are present.
        # Buckets are mutable but an existing small pointer can remain cached. Recreate it
        # after the immutable generation is complete rather than relying on an overwrite.
        batch_bucket_files(BUCKET, delete=[CURRENT_PATH], token=token)
        batch_bucket_files(BUCKET, add=[(str(manifest_path), CURRENT_PATH)], token=token)

        stale_paths = sorted(
            item.path
            for item in list_bucket_tree(BUCKET, prefix="generations", recursive=True, token=token)
            if item.type == "file" and not item.path.startswith(prefix + "/")
        )
        if stale_paths:
            batch_bucket_files(BUCKET, delete=stale_paths, token=token)
            print("Deleted previous or incomplete generation files", flush=True)
        print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)
        return 0
    finally:
        if args.keep_workdir:
            print(f"Preserved work directory: {workdir}", flush=True)
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
