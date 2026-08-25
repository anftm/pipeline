#!/usr/bin/env python3
"""Build a deterministic queue for changed files requiring reader conversion."""

import argparse
import json
import os
from pathlib import Path

from huggingface_hub import HfApi
from huggingface_hub.errors import RepositoryNotFoundError

try:
    from .reader_assets import (
        CONVERTIBLE_EXTENSIONS, MANIFEST_NAME, READER_ASSETS_REPO, asset_key, conversion_contract,
        canonical_json, decode_search_payload, empty_manifest, load_json,
        relative_path, source_url, validate_manifest,
    )
except ImportError:
    from reader_assets import (
        CONVERTIBLE_EXTENSIONS, MANIFEST_NAME, READER_ASSETS_REPO, asset_key, conversion_contract,
        canonical_json, decode_search_payload, empty_manifest, load_json,
        relative_path, source_url, validate_manifest,
    )


def remote_manifest(api: HfApi, repo_id: str) -> dict:
    try:
        if not api.file_exists(repo_id=repo_id, repo_type="dataset", filename=MANIFEST_NAME):
            return empty_manifest()
    except RepositoryNotFoundError:
        return empty_manifest()
    path = api.hf_hub_download(repo_id=repo_id, repo_type="dataset", filename=MANIFEST_NAME)
    return validate_manifest(load_json(Path(path)))


def build_queue(records, revisions, manifest, *, repo="", extension="", exact_path="", limit=0,
                 retry_failed=False, force=False) -> list[dict]:
    selected = []
    extension = extension.lower().lstrip(".")
    files = manifest.get("files", {})
    for record in records:
        source_repo = str(record.get("Repo") or "")
        ext = str(record.get("Extension") or "").lower().lstrip(".")
        if ext not in CONVERTIBLE_EXTENSIONS or (repo and source_repo != repo) or (extension and ext != extension):
            continue
        revision = str(revisions.get(source_repo) or "")
        if not revision:
            continue
        path = relative_path(record)
        if exact_path and path != exact_path:
            continue
        key = asset_key(source_repo, path)
        profile, reader_mode, output_name = conversion_contract(ext, key)
        existing = files.get(key, {})
        manual = str(existing.get("profile") or "").startswith("manual-")
        if not force and existing.get("source_revision") == revision and existing.get("status") == "ready" and manual:
            continue
        current = existing.get("source_revision") == revision and existing.get("profile") == profile
        if not force and current and existing.get("status") == "ready":
            continue
        if not force and current and existing.get("status") == "failed" and not retry_failed:
            continue
        selected.append({
            "key": key, "repo": source_repo, "path": path, "extension": ext,
            "source_revision": revision, "source_bytes": record.get("Size") or 0,
            "source_url": source_url(source_repo, revision, path), "profile": profile,
            "reader_mode": reader_mode, "output_name": output_name,
        })
    selected.sort(key=lambda item: (item["repo"], item["path"]))
    return selected[:limit] if limit > 0 else selected


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--search-data", type=Path, default=Path("output/search_data.json"))
    parser.add_argument("--revisions", type=Path, default=Path("state/commits.json"))
    parser.add_argument("--output", type=Path, default=Path("output/reader-assets/queue.json"))
    parser.add_argument("--repo", default="")
    parser.add_argument("--extension", default="")
    parser.add_argument("--path", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--assets-repo", default=os.environ.get("READER_ASSETS_REPO", READER_ASSETS_REPO))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.limit < 0:
        raise ValueError("limit must be non-negative")
    records = decode_search_payload(load_json(args.search_data))
    revisions = load_json(args.revisions)
    if args.manifest:
        manifest = validate_manifest(load_json(args.manifest))
    else:
        manifest = remote_manifest(HfApi(token=os.environ.get("HF_TOKEN") or None), args.assets_repo)
    queue = build_queue(records, revisions, manifest, repo=args.repo, extension=args.extension, exact_path=args.path,
                        limit=args.limit, retry_failed=args.retry_failed, force=args.force)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical_json({"version": 1, "items": queue}, pretty=True))
    print(f"queued {len(queue)} reader asset conversion(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
