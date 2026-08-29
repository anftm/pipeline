#!/usr/bin/env python3
"""Atomically publish converted Reader Assets and their manifest."""

import argparse
import hashlib
import os
import random
import time
from datetime import date
from pathlib import Path

from huggingface_hub import CommitOperationAdd, HfApi
from huggingface_hub.errors import HfHubHTTPError, RepositoryNotFoundError

try:
    from .build_reader_assets_index import encode_index
    from .reader_assets import (
        MANIFEST_NAME, READER_ASSETS_REPO, canonical_json, empty_manifest, load_json,
        reusable_object_key, validate_manifest, validate_object_path,
    )
except ImportError:
    from build_reader_assets_index import encode_index
    from reader_assets import (
        MANIFEST_NAME, READER_ASSETS_REPO, canonical_json, empty_manifest, load_json,
        reusable_object_key, validate_manifest, validate_object_path,
    )

SIDECAR_NAME = "reader_assets.json.gz"


def orphan_entry(entry: dict) -> dict:
    orphan = {field: entry[field] for field in (
        "source_sha256", "profile", "reader_mode", "path", "bytes", "sha256"
    ) if field in entry}
    orphan["since"] = date.today().isoformat()
    return orphan


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def remote_manifest(api: HfApi, repo_id: str, revision: str | None = None) -> dict:
    try:
        if not api.file_exists(
                repo_id=repo_id, repo_type="dataset", filename=MANIFEST_NAME, revision=revision):
            return empty_manifest()
    except RepositoryNotFoundError:
        return empty_manifest()
    path = api.hf_hub_download(
        repo_id=repo_id, repo_type="dataset", filename=MANIFEST_NAME, revision=revision,
    )
    return validate_manifest(load_json(Path(path)))


def build_publish(api: HfApi, repo_id: str, bundle: Path, revision: str | None = None):
    data = load_json(bundle / "bundle.json")
    if data.get("version") != 1 or not isinstance(data.get("results"), list):
        raise ValueError("invalid reader asset bundle")
    manifest = remote_manifest(api, repo_id, revision)
    files = dict(manifest["files"])
    orphans = dict(manifest.get("orphans", {}))
    reusable = {}
    candidates = list(files.items()) + [("", entry) for entry in orphans.values()]
    for key, candidate in candidates:
        if (candidate.get("status", "ready") == "ready" and candidate.get("source_sha256")
                and candidate.get("profile") and candidate.get("path") and candidate.get("sha256")
                and isinstance(candidate.get("bytes"), int) and candidate["bytes"] > 0
                and candidate.get("reader_mode")):
            extension = candidate.get("source_extension", "")
            if not key and extension in {"htm", "html"}:
                continue
            identity = reusable_object_key(
                candidate["source_sha256"], candidate["profile"], extension=extension,
                source_revision=candidate.get("source_revision", ""), key=key,
            )
            reusable[identity] = candidate
    artifacts = {}
    for result in data["results"]:
        entry = {key: value for key, value in result.items() if key != "key"}
        if result.get("status") == "ready":
            validate_object_path(result.get("path"))
            remote = None
            if not data.get("force_rebuild"):
                identity = reusable_object_key(
                    result.get("source_sha256", ""), result.get("profile", ""),
                    extension=result.get("source_extension", ""),
                    source_revision=result.get("source_revision", ""), key=result.get("key", ""),
                )
                remote = reusable.get(identity)
            if remote:
                for field in ("path", "bytes", "sha256", "reader_mode"):
                    entry[field] = remote[field]
                entry.pop("reused", None)
            else:
                artifact = bundle / result["path"]
                if not artifact.is_file() or artifact.stat().st_size != result["bytes"]:
                    raise ValueError(f"missing or invalid artifact for {result['key']}")
                if file_sha256(artifact) != result["sha256"]:
                    raise ValueError(f"artifact digest mismatch for {result['key']}")
                artifacts[result["path"]] = str(artifact)
                if result.get("chapter_manifest"):
                    prefix = Path(result["chapter_manifest"]).parent
                    root = bundle / prefix
                    if not root.is_dir():
                        raise ValueError(f"missing EPUB chapter bundle for {result['key']}")
                    for child in sorted(root.rglob("*")):
                        if child.is_file():
                            path = (prefix / child.relative_to(root)).as_posix()
                            validate_object_path(path)
                            artifacts[path] = str(child)
        elif result.get("status") != "failed":
            raise ValueError("unknown reader asset result status")
        elif files.get(result["key"], {}).get("status") == "ready":
            previous = dict(files[result["key"]])
            previous.update({
                "failed_source_revision": result.get("source_revision", ""),
                "failed_profile": result.get("profile", ""),
                "failed_error": result.get("error", "RuntimeError"),
            })
            files[result["key"]] = previous
            continue
        entry.pop("reused", None)
        previous = files.get(result["key"], {})
        if (previous.get("status") == "ready" and previous.get("path")
                and previous["path"] != entry.get("path")):
            orphans.setdefault(previous["path"], orphan_entry(previous))
        files[result["key"]] = entry
        orphans.pop(entry.get("path", ""), None)
        if data.get("force_rebuild") and entry.get("status") == "ready":
            for key, candidate in list(files.items()):
                if candidate.get("status") == "ready" and candidate.get("path") == entry["path"]:
                    files[key] = {
                        **candidate,
                        "bytes": entry["bytes"],
                        "sha256": entry["sha256"],
                        "reader_mode": entry["reader_mode"],
                    }
            for path, candidate in list(orphans.items()):
                if candidate.get("path", path) == entry["path"]:
                    orphans[path] = {
                        **candidate,
                        "bytes": entry["bytes"],
                        "sha256": entry["sha256"],
                        "reader_mode": entry["reader_mode"],
                    }
    active_keys = set(data.get("active_keys", []))
    if data.get("authoritative_snapshot") is True:
        for key in set(files) - active_keys:
            removed = files.pop(key)
            if removed.get("status") == "ready" and removed.get("path"):
                orphans.setdefault(removed["path"], orphan_entry(removed))
    referenced = {entry.get("path") for entry in files.values() if entry.get("status") == "ready"}
    orphans = {path: entry for path, entry in orphans.items() if path not in referenced}
    updated = {
        "version": 1,
        "files": dict(sorted(files.items())),
        "orphans": dict(sorted(orphans.items())),
    }
    validate_manifest(updated)
    operations = [
        CommitOperationAdd(path_in_repo=path, path_or_fileobj=source)
        for path, source in sorted(artifacts.items())
    ]
    operations.append(CommitOperationAdd(path_in_repo=MANIFEST_NAME, path_or_fileobj=canonical_json(updated, pretty=True)))
    operations.append(CommitOperationAdd(path_in_repo=SIDECAR_NAME, path_or_fileobj=encode_index(updated)))
    return updated, operations


def publish_bundle(api: HfApi, repo_id: str, bundle: Path, *, max_attempts: int = 20) -> tuple[dict, int]:
    data = load_json(bundle / "bundle.json")
    result_keys = {result.get("key") for result in data.get("results", []) if result.get("key")}
    baseline = None
    for attempt in range(max_attempts):
        revision = api.repo_info(repo_id=repo_id, repo_type="dataset").sha
        current = remote_manifest(api, repo_id, revision)
        current_entries = {key: current["files"].get(key) for key in result_keys}
        if baseline is None:
            baseline = current_entries
        elif current_entries != baseline:
            raise RuntimeError("reader asset key changed during publication retry")
        manifest, operations = build_publish(api, repo_id, bundle, revision)
        try:
            api.create_commit(
                repo_id=repo_id, repo_type="dataset", operations=operations,
                commit_message="Update reader assets", parent_commit=revision,
            )
            return manifest, len(operations) - 2
        except HfHubHTTPError as exc:
            if getattr(exc.response, "status_code", None) not in {409, 412} or attempt + 1 == max_attempts:
                raise
            print(f"reader asset parent changed; rebuilding publication ({attempt + 2}/{max_attempts})")
            time.sleep(random.uniform(0.5, min(8.0, 0.5 * (attempt + 1))))
    raise RuntimeError("reader asset publication retry limit reached")


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, default=Path("output/reader-assets/bundle"))
    parser.add_argument("--assets-repo", default=os.environ.get("READER_ASSETS_REPO", READER_ASSETS_REPO))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = os.environ.get("HF_TOKEN", "")
    if not token and not args.dry_run:
        raise RuntimeError("HF_TOKEN is required")
    api = HfApi(token=token or None)
    if not args.dry_run:
        api.create_repo(repo_id=args.assets_repo, repo_type="dataset", exist_ok=True)
    if args.dry_run:
        manifest, operations = build_publish(api, args.assets_repo, args.bundle)
        print(f"dry run: validated {len(operations) - 2} artifact(s), {len(manifest['files'])} manifest entries")
        return 0
    _, artifact_count = publish_bundle(api, args.assets_repo, args.bundle)
    print(f"published {artifact_count} artifact(s) to {args.assets_repo}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
