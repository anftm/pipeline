#!/usr/bin/env python3
"""Update the changing CCRD source TXT in the Search Space."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from ccrd_source_override import SPACE_PATH, download_override


SPACE_REPO = "VoiceOfML/Search"


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
        target = temporary / SPACE_PATH
        if not target.is_file():
            raise RuntimeError(f"Space lacks legacy CCRD target path: {SPACE_PATH}")
        details = download_override(target)
        run(["git", "config", "user.name", "github-actions[bot]"], temporary)
        run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], temporary)
        run(["git", "add", str(SPACE_PATH)], temporary)
        changed = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=temporary).returncode != 0
        if not changed:
            print("CCRD source TXT is already current")
            return 0
        run(["git", "commit", "-m", "chore: update CCRD source text [skip ci]"], temporary)
        run(["git", "push"], temporary)
        print(f"Updated CCRD source: {details['url']} ({details['sha256']})")
        return 0
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
