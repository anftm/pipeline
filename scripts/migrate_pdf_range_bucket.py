#!/usr/bin/env python3
"""Move legacy structure-optimized PDFs out of Reader-Assets.

The migration uploads and verifies every referenced object in the dedicated
Bucket before deleting the old Dataset objects.  The Reader sidecar is updated
in the same parent-checked commit, so a PDF is never routed to the new Bucket
before its object is present there.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from huggingface_hub import HfApi, HfFileSystem, hf_hub_download, sync_bucket

try:
    from . import pdf_range_state, shared
    from .build_reader_assets_index import encode_index
    from .publish_reader_assets import remote_manifest, remote_pdf_manifest, remote_pdf_ocr_manifest
    from .reader_assets import READER_ASSETS_REPO, canonical_json
    from huggingface_hub import CommitOperationAdd, CommitOperationDelete
except ImportError:
    import pdf_range_state, shared
    from build_reader_assets_index import encode_index
    from publish_reader_assets import remote_manifest, remote_pdf_manifest, remote_pdf_ocr_manifest
    from reader_assets import READER_ASSETS_REPO, canonical_json
    from huggingface_hub import CommitOperationAdd, CommitOperationDelete


def _digest_stream(stream) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(shared.CHUNK_BYTES):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def legacy_objects(state: dict, manifest: dict, limit: int, checkpoint: int) -> list[dict]:
    """Return unique old objects, excluding any ordinary asset using the path."""
    protected = {
        entry.get("path") for entry in manifest.get("files", {}).values()
        if isinstance(entry, dict) and entry.get("status") == "ready"
    }
    selected = {}
    for entry in state.get("files", {}).values():
        if (not isinstance(entry, dict) or entry.get("status") != "optimized"
                or pdf_range_state.artifact_bucket(state, entry) == shared.PDF_RANGE_BUCKET):
            continue
        path = entry.get("path")
        if (isinstance(path, str) and path.startswith("objects/")
                and path.endswith("/document.pdf") and ".." not in path.split("/")
                and path not in protected):
            selected.setdefault(path, {"path": path, "sha256": entry.get("sha256"),
                                       "bytes": entry.get("bytes")})
    values = [selected[path] for path in sorted(selected)]
    start = checkpoint * limit if limit else 0
    return values[start:start + limit if limit else None]


def verify_bucket(fs: HfFileSystem, path: str, expected_sha: str | None, expected_bytes: int | None) -> None:
    with fs.open(f"hf://buckets/{shared.PDF_RANGE_BUCKET}/{path}", "rb") as stream:
        digest, size = _digest_stream(stream)
    if expected_sha and digest != expected_sha:
        raise ValueError(f"Bucket SHA-256 mismatch: {path}")
    if isinstance(expected_bytes, int) and size != expected_bytes:
        raise ValueError(f"Bucket byte-size mismatch: {path}")


def migrate(api: HfApi, repo: str, *, limit: int = 100, checkpoint: int = 0,
            apply: bool = False) -> dict:
    revision = api.repo_info(repo_id=repo, repo_type="dataset").sha
    state = pdf_range_state.remote_state(api, repo, revision)
    manifest = remote_manifest(api, repo, revision)
    selected = legacy_objects(state, manifest, limit, checkpoint)
    report = {"revision": revision, "selected": len(selected), "objects": selected,
              "bucket": shared.PDF_RANGE_BUCKET, "apply": apply}
    if not apply or not selected:
        return report
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise RuntimeError("HF_TOKEN is required for migration")
    with tempfile.TemporaryDirectory(prefix="pdf-range-migration-") as root:
        root_path = Path(root)
        include = []
        for item in selected:
            local = Path(hf_hub_download(repo_id=repo, repo_type="dataset",
                                          filename=item["path"], revision=revision, token=token))
            with local.open("rb") as stream:
                digest, size = _digest_stream(stream)
            if item.get("sha256") and digest != item["sha256"]:
                raise ValueError(f"Reader-Assets SHA-256 mismatch: {item['path']}")
            if isinstance(item.get("bytes"), int) and size != item["bytes"]:
                raise ValueError(f"Reader-Assets byte-size mismatch: {item['path']}")
            target = root_path / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            with local.open("rb") as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination, length=shared.CHUNK_BYTES)
            include.append(item["path"])
        sync_bucket(str(root_path), f"hf://buckets/{shared.PDF_RANGE_BUCKET}",
                    include=include, token=token, quiet=False)
    fs = HfFileSystem(token=token)
    for item in selected:
        verify_bucket(fs, item["path"], item.get("sha256"), item.get("bytes"))

    current_revision = api.repo_info(repo_id=repo, repo_type="dataset").sha
    if current_revision != revision:
        raise RuntimeError("Reader-Assets changed during migration; rerun from the new revision")
    current_state = pdf_range_state.remote_state(api, repo, revision)
    if current_state != state:
        raise RuntimeError("PDF range state changed during migration; rerun")
    updated_files = dict(state.get("files", {}))
    for entry in selected:
        for key, value in updated_files.items():
            if isinstance(value, dict) and value.get("path") == entry["path"]:
                updated_files[key] = {**value, "artifact_bucket": shared.PDF_RANGE_BUCKET}
    updated_state = {**state, "files": updated_files}
    if not pdf_range_state.has_legacy_artifacts(updated_state):
        updated_state["artifact_bucket"] = shared.PDF_RANGE_BUCKET
    pdf_manifest = remote_pdf_manifest(api, repo, revision)
    ocr_manifest = remote_pdf_ocr_manifest(api, repo, revision)
    sidecar = encode_index(manifest, pdf_manifest, updated_state, ocr_manifest)
    operations = [CommitOperationDelete(path_in_repo=item["path"]) for item in selected]
    operations.extend([
        CommitOperationAdd(path_in_repo=pdf_range_state.MANIFEST_NAME,
                           path_or_fileobj=canonical_json(updated_state, pretty=True)),
        CommitOperationAdd(path_in_repo="reader_assets.json.gz", path_or_fileobj=sidecar),
    ])
    api.create_commit(repo_id=repo, repo_type="dataset", operations=operations,
                      commit_message="Move optimized PDFs to dedicated Bucket",
                      parent_commit=revision)
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets-repo", default=os.environ.get("READER_ASSETS_REPO", READER_ASSETS_REPO))
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--checkpoint", type=int, default=0)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("output/pdf-range-migration/report.json"))
    args = parser.parse_args()
    if args.limit < 1 or args.checkpoint < 0:
        raise ValueError("limit must be positive and checkpoint must be non-negative")
    if args.apply and not os.environ.get("HF_TOKEN"):
        raise RuntimeError("HF_TOKEN is required for migration")
    report = migrate(HfApi(token=os.environ.get("HF_TOKEN")), args.assets_repo,
                     limit=args.limit, checkpoint=args.checkpoint, apply=args.apply)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                           encoding="utf-8")
    print(f"selected={report['selected']} apply={args.apply}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
