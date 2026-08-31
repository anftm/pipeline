#!/usr/bin/env python3
"""Remove non-published skipped decisions from the PDF asset manifest."""

import json
import os
from pathlib import Path

from huggingface_hub import CommitOperationAdd, HfApi


MANIFEST_NAME = "pdf_manifest.json"
REPO = os.environ.get("READER_ASSETS_REPO", "vomebook/Reader-Assets")


def main() -> int:
    token = os.environ.get("HF_TOKEN", "")
    if not token:
        raise RuntimeError("HF_TOKEN is required")
    api = HfApi(token=token)
    revision = api.repo_info(repo_id=REPO, repo_type="dataset").sha
    local = Path(api.hf_hub_download(repo_id=REPO, repo_type="dataset", filename=MANIFEST_NAME, revision=revision))
    manifest = json.loads(local.read_text(encoding="utf-8"))
    files = manifest.get("files", {})
    kept = {key: entry for key, entry in files.items() if entry.get("status") != "skipped"}
    removed = len(files) - len(kept)
    if not removed:
        print("no skipped PDF manifest entries")
        return 0
    updated = {**manifest, "files": dict(sorted(kept.items()))}
    payload = (json.dumps(updated, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    api.create_commit(
        repo_id=REPO, repo_type="dataset",
        operations=[CommitOperationAdd(path_in_repo=MANIFEST_NAME, path_or_fileobj=payload)],
        commit_message="Prune skipped PDF manifest entries", parent_commit=revision,
    )
    print(f"removed {removed} skipped PDF manifest entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
