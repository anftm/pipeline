#!/usr/bin/env python3
"""Migrate a bounded batch of bucket PDF page manifests from v1 to v2."""

import argparse
import json
import os
import tempfile
import time
from pathlib import Path

from huggingface_hub import CommitOperationAdd, HfApi, batch_bucket_files, download_bucket_files
from huggingface_hub.errors import HfHubHTTPError

try:
    from . import pdf_assets
except ImportError:
    import pdf_assets


BUCKET = "vomebook/pdf-pages"
MAX_BATCH = 100
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def _retry(operation, label: str, max_attempts: int = 6):
    for attempt in range(max_attempts):
        try:
            return operation()
        except HfHubHTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status not in RETRYABLE_STATUSES or attempt + 1 == max_attempts:
                raise
        except (ConnectionError, OSError):
            if attempt + 1 == max_attempts:
                raise
        delay = min(60, 2 ** attempt)
        print(f"transient {label} error; retrying in {delay}s", flush=True)
        time.sleep(delay)
    raise RuntimeError(f"{label} retry limit reached")


def load_manifest(api: HfApi, repo: str, revision: str) -> dict:
    path = _retry(lambda: api.hf_hub_download(
        repo_id=repo, repo_type="dataset", filename=pdf_assets.MANIFEST_NAME,
        revision=revision, force_download=True), "dataset manifest download")
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    if manifest.get("version") != pdf_assets.MANIFEST_VERSION or not isinstance(manifest.get("files"), dict):
        raise ValueError("invalid dataset PDF manifest")
    return manifest


def convert_page_manifest(data: dict, entry: dict, manifest_path: str) -> dict:
    version = data.get("version")
    if data.get("kind") != "pdf-pages":
        raise ValueError(f"invalid page manifest kind: {manifest_path}")
    if version == pdf_assets.PAGE_MANIFEST_VERSION:
        expected = {"version", "kind", "source_sha256", "profile", "page_count", "toc"}
        if set(data) - expected:
            raise ValueError(f"unexpected v2 page manifest fields: {manifest_path}")
        compact = data
    elif version == 1:
        pages = data.get("pages")
        if not isinstance(pages, list):
            raise ValueError(f"invalid v1 page manifest pages: {manifest_path}")
        compact = pdf_assets.compact_page_manifest(
            str(data.get("source_sha256") or ""), str(data.get("profile") or ""),
            pages, data.get("toc"), Path(manifest_path).parent)
    else:
        raise ValueError(f"unsupported page manifest version: {manifest_path}")
    if (compact.get("source_sha256") != entry.get("source_sha256")
            or compact.get("profile") != entry.get("render_profile")
            or type(compact.get("page_count")) is not int
            or compact["page_count"] < 1):
        raise ValueError(f"page manifest does not match dataset metadata: {manifest_path}")
    if "toc" in compact and not isinstance(compact["toc"], list):
        raise ValueError(f"invalid page manifest toc: {manifest_path}")
    return compact


def select_batch(manifest: dict, limit: int) -> list[tuple[str, dict]]:
    if not 1 <= limit <= MAX_BATCH:
        raise ValueError(f"limit must be between 1 and {MAX_BATCH}")
    selected = []
    for key, entry in sorted(manifest["files"].items()):
        descriptor = entry.get("page_manifest") if isinstance(entry, dict) else None
        if (entry.get("status") == "ready" and entry.get("strategy") == "sampled-webp"
                and isinstance(descriptor, dict) and descriptor.get("path")
                and descriptor.get("version") != pdf_assets.PAGE_MANIFEST_VERSION):
            selected.append((key, entry))
            if len(selected) == limit:
                break
    return selected


def _check_revision(api: HfApi, repo: str, revision: str) -> None:
    current = _retry(lambda: api.repo_info(repo_id=repo, repo_type="dataset"), "revision check")
    if current.sha != revision:
        raise RuntimeError(f"dataset revision changed: expected {revision}, found {current.sha}")


def migrate(api: HfApi, repo: str, revision: str, limit: int, apply: bool,
            token: str | None, bucket: str = BUCKET) -> int:
    _check_revision(api, repo, revision)
    manifest = load_manifest(api, repo, revision)
    selected = select_batch(manifest, limit)
    if not selected:
        print("no v1 PDF page manifests remain")
        return 0

    with tempfile.TemporaryDirectory(prefix="pdf_manifest_v2_") as root:
        root_path = Path(root)
        downloads = []
        for index, (_key, entry) in enumerate(selected):
            downloads.append((entry["page_manifest"]["path"], root_path / f"{index}.json"))
        _retry(lambda: download_bucket_files(
            bucket, files=downloads, token=token, raise_on_missing_files=True), "bucket manifest download")

        uploads = []
        updated = {**manifest, "files": dict(manifest["files"])}
        for index, (key, entry) in enumerate(selected):
            local = root_path / f"{index}.json"
            compact = convert_page_manifest(
                json.loads(local.read_text(encoding="utf-8")), entry, entry["page_manifest"]["path"])
            local.write_text(json.dumps(compact, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                             encoding="utf-8")
            sha256, size = pdf_assets.digest(local)
            target_path = (Path(entry["page_manifest"]["path"]).parent / pdf_assets.PAGE_MANIFEST_NAME).as_posix()
            descriptor = {**entry["page_manifest"], "path": target_path, "sha256": sha256, "bytes": size,
                          "version": pdf_assets.PAGE_MANIFEST_VERSION}
            updated["files"][key] = {
                **{field: value for field, value in entry.items() if field not in {"pages", "outline"}},
                "page_manifest": descriptor,
            }
            uploads.append((str(local), target_path))

        print(f"planned {len(selected)} PDF page manifest migration(s) at dataset revision {revision}")
        if not apply:
            return len(selected)
        if not token:
            raise RuntimeError("HF_TOKEN is required with --apply")

        _check_revision(api, repo, revision)
        _retry(lambda: batch_bucket_files(bucket, add=uploads, token=token), "bucket manifest upload")
        _check_revision(api, repo, revision)
        payload = json.dumps(updated, ensure_ascii=False, sort_keys=True, indent=2).encode()
        operation = CommitOperationAdd(path_in_repo=pdf_assets.MANIFEST_NAME, path_or_fileobj=payload)
        _retry(lambda: api.create_commit(
            repo_id=repo, repo_type="dataset", operations=[operation],
            commit_message="Migrate PDF page manifests to v2", parent_commit=revision),
            "dataset metadata update")
        print(f"migrated {len(selected)} PDF page manifest(s)")
        return len(selected)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets-repo", default=os.environ.get("READER_ASSETS_REPO", pdf_assets.READER_ASSETS_REPO))
    parser.add_argument("--bucket", default=BUCKET)
    parser.add_argument("--revision", required=True, help="Exact dataset commit SHA to check and update")
    parser.add_argument("--limit", type=int, required=True, help=f"Batch size (1-{MAX_BATCH})")
    parser.add_argument("--apply", action="store_true", help="Upload manifests and update dataset metadata")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = os.environ.get("HF_TOKEN")
    migrate(HfApi(token=token), args.assets_repo, args.revision, args.limit, args.apply, token, args.bucket)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
