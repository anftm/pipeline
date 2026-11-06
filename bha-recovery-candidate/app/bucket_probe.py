"""Manual Elasticsearch snapshot/restore probe for a mounted HF Storage Bucket."""

from __future__ import annotations

import sys
import shutil
import uuid
from pathlib import Path

from elasticsearch import Elasticsearch

from .config import DATA_ROOT, ES_URL

LOCAL_REPOSITORY = DATA_ROOT / "es-repositories" / "probe"


def clear_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m app.bucket_probe <bucket-dir>")
    bucket_dir = Path(sys.argv[1])
    if not bucket_dir.is_dir():
        raise RuntimeError(f"bucket directory is unavailable: {bucket_dir}")
    repository = "bha_bucket_probe"
    index = f"bha-bucket-probe-{uuid.uuid4().hex[:12]}"
    snapshot = f"probe-{uuid.uuid4().hex[:12]}"
    bucket_copy = bucket_dir / "probe" / snapshot
    client = Elasticsearch(ES_URL, request_timeout=120)
    try:
        clear_directory(LOCAL_REPOSITORY)
        client.indices.create(index=index)
        client.index(index=index, id="1", document={"probe": "bucket"}, refresh="wait_for")
        client.snapshot.create_repository(
            name=repository,
            repository={"type": "fs", "settings": {"location": str(LOCAL_REPOSITORY)}},
            verify=True,
        )
        client.snapshot.create(repository=repository, snapshot=snapshot, indices=index, wait_for_completion=True)
        shutil.copytree(LOCAL_REPOSITORY, bucket_copy)
        client.indices.delete(index=index)
        clear_directory(LOCAL_REPOSITORY)
        shutil.copytree(bucket_copy, LOCAL_REPOSITORY, dirs_exist_ok=True)
        client.snapshot.create_repository(
            name=repository,
            repository={"type": "fs", "settings": {"location": str(LOCAL_REPOSITORY)}},
            verify=True,
        )
        client.snapshot.restore(repository=repository, snapshot=snapshot, indices=index, wait_for_completion=True)
        if int(client.count(index=index)["count"]) != 1:
            raise RuntimeError("restored probe index has the wrong document count")
        print("bha_bucket_snapshot_probe=ready", flush=True)
        return 0
    finally:
        client.indices.delete(index=index, ignore_unavailable=True)
        shutil.rmtree(bucket_copy, ignore_errors=True)
        clear_directory(LOCAL_REPOSITORY)


if __name__ == "__main__":
    raise SystemExit(main())
