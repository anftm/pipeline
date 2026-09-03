#!/usr/bin/env python3
"""Retire linearized PDFs and legacy page streams before the WebP-only rebuild."""

import argparse
import json
import os
from pathlib import Path

from huggingface_hub import CommitOperationAdd, CommitOperationDelete, HfApi

try:
    from . import pdf_assets
    from .build_reader_assets_index import encode_index
    from .publish_reader_assets import remote_manifest, remote_pdf_manifest
except ImportError:
    import pdf_assets
    from build_reader_assets_index import encode_index
    from publish_reader_assets import remote_manifest, remote_pdf_manifest


def retire(reader_manifest: dict, pdf_manifest: dict) -> tuple[dict, bytes, list[CommitOperationDelete]]:
    files = dict(pdf_manifest.get("files", {}))
    delete_paths = set()
    for key, entry in list(files.items()):
        linearized = entry.get("status") == "ready" and entry.get("strategy") == "linearized-pdf"
        legacy_pages = (entry.get("status") == "ready" and entry.get("strategy") == "sampled-webp"
                        and (entry.get("render_profile") != pdf_assets.PDF_PROFILE
                             or entry.get("decision_profile") != pdf_assets.PDF_DECISION_PROFILE))
        if not linearized and not legacy_pages:
            continue
        if linearized and entry.get("path"):
            delete_paths.add(entry["path"])
        if legacy_pages:
            manifest_path = entry.get("page_manifest", {}).get("path")
            if manifest_path:
                delete_paths.add(manifest_path)
                delete_paths.add((Path(manifest_path).parent / "pages").as_posix())
        retired = {k: v for k, v in entry.items()
                   if k not in {"path", "pages", "page_manifest", "bytes", "sha256", "pdf",
                                "render_profile", "decision_profile"}}
        retired.update({"status": "retired", "strategy": "none", "reason": "independent-pdf-asset-retired"})
        files[key] = retired
    updated = {"version": 1, "files": dict(sorted(files.items()))}
    sidecar = encode_index(reader_manifest, updated)
    deletes = [CommitOperationDelete(path_in_repo=path, is_folder=path.endswith("/pages"))
               for path in sorted(delete_paths)]
    return updated, sidecar, deletes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets-repo", default=os.environ.get("READER_ASSETS_REPO", pdf_assets.READER_ASSETS_REPO))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    token = os.environ.get("HF_TOKEN", "")
    if not token:
        raise RuntimeError("HF_TOKEN is required")
    api = HfApi(token=token)
    revision = api.repo_info(repo_id=args.assets_repo, repo_type="dataset").sha
    reader_manifest = remote_manifest(api, args.assets_repo, revision)
    pdf_manifest = remote_pdf_manifest(api, args.assets_repo, revision)
    updated, sidecar, deletes = retire(reader_manifest, pdf_manifest)
    retired = sum(entry.get("status") == "retired" for entry in updated["files"].values())
    print(f"retiring {retired} PDF asset mapping(s) and {len(deletes)} object path(s)")
    if not args.apply:
        return 0
    operations = [*deletes,
                  CommitOperationAdd(path_in_repo=pdf_assets.MANIFEST_NAME,
                                     path_or_fileobj=json.dumps(updated, ensure_ascii=False, sort_keys=True,
                                                                indent=2).encode()),
                  CommitOperationAdd(path_in_repo="reader_assets.json.gz", path_or_fileobj=sidecar)]
    api.create_commit(repo_id=args.assets_repo, repo_type="dataset", parent_commit=revision,
                      operations=operations, commit_message="Retire legacy independent PDF assets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
