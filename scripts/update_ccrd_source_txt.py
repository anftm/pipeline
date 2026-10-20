#!/usr/bin/env python3
"""Update the changing CCRD source TXT in the Search Space."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from huggingface_hub import hf_hub_download

from ccrd_source_override import SPACE_PATH, migrate_space, publish_source


SPACE_REPO = "vomebook/Search"
METADATA_FILES = (
    "data/search_data.json.gz",
    "data/folder_tree.json.gz",
    "data/folder_browser.json.gz",
    "data/sources.json.gz",
)


def materialize_metadata(root: Path, token: str) -> None:
    """Replace skipped-LFS pointers with the real Space metadata bytes."""
    for relative in METADATA_FILES:
        cached = hf_hub_download(
            repo_id=SPACE_REPO,
            repo_type="space",
            filename=relative,
            token=token,
        )
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cached, target)


def run(command: list[str], cwd: Path) -> None:
    environment = os.environ.copy()
    environment["GIT_LFS_SKIP_SMUDGE"] = "1"
    subprocess.run(command, cwd=cwd, env=environment, check=True)


def main() -> int:
    token = os.environ.get("HF_TOKEN", "")
    username = os.environ.get("HF_USERNAME", "VoiceOfML")
    if not token:
        raise RuntimeError("HF_TOKEN is required")

    temporary = Path(tempfile.mkdtemp(prefix="ccrd-source-update-"))
    try:
        clone_url = f"https://{username}:{token}@huggingface.co/spaces/{SPACE_REPO}"
        run(["git", "clone", "--depth", "1", clone_url, str(temporary)], temporary.parent)
        materialize_metadata(temporary, token)
        details = publish_source(temporary)
        counts = migrate_space(temporary)
        if not (temporary / SPACE_PATH).is_file():
            raise RuntimeError(f"Space lacks published CCRD target path: {SPACE_PATH}")
        print(f"CW documents after migration: {counts['CW']}")
        run(["git", "lfs", "install", "--local"], temporary)
        run(["git", "config", "user.name", "github-actions[bot]"], temporary)
        run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], temporary)
        run(["git", "add", "-A", "CW", "data/search_data.json.gz", "data/folder_tree.json.gz",
             "data/folder_browser.json.gz", "data/sources.json.gz", "data/initial", "data/sidebar"], temporary)
        changed = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=temporary).returncode != 0
        if not changed:
            print("CCRD source TXT and CW layout are already current")
            return 0
        run(["git", "commit", "-m", "chore: update CCRD source text and CW layout"], temporary)
        run(["git", "push"], temporary)
        print(f"Updated CCRD source: {details['url']} ({details['sha256']})")
        return 0
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
