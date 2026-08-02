#!/usr/bin/env python3
"""Compare anftm parsed branch revisions and write a candidate state file."""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


OWNER = os.environ.get("BHA_ARCHIVE_OWNER", "anftm")
REPO_START = int(os.environ.get("REPO_START", "0"))
REPO_END = int(os.environ.get("REPO_END", "31"))
STATE_PATH = Path(os.environ.get("BHA_REVISION_STATE", "state/bha-parsed-revisions.json"))
CANDIDATE_PATH = Path(os.environ.get("BHA_REVISION_CANDIDATE", "/tmp/bha-parsed-revisions.json"))
GITHUB_OUTPUT = os.environ.get("GITHUB_OUTPUT")


def revision(token: str, archive_id: int) -> str:
    repo = f"banned-historical-archives{archive_id}"
    owner = urllib.parse.quote(OWNER)
    url = f"https://api.github.com/repos/{owner}/{urllib.parse.quote(repo)}/git/ref/heads/parsed"
    request = urllib.request.Request(url)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "anftm-pipeline-bha-revisions/1.0")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{repo}/parsed lookup failed with HTTP {exc.code}") from exc
    value = str(data.get("object", {}).get("sha") or "")
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise RuntimeError(f"{repo}/parsed returned an invalid revision")
    return value


def output(name: str, value: str) -> None:
    if GITHUB_OUTPUT:
        with open(GITHUB_OUTPUT, "a", encoding="utf-8") as target:
            target.write(f"{name}={value}\n")
    print(f"{name}={value}")


def main() -> None:
    token = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN", "")
    current = {
        str(archive_id): revision(token, archive_id)
        for archive_id in range(REPO_START, REPO_END + 1)
    }
    try:
        previous = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        previous = None
    CANDIDATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CANDIDATE_PATH.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    baseline = not isinstance(previous, dict)
    changed_ids = [key for key in current if isinstance(previous, dict) and previous.get(key) != current[key]]
    output("baseline", str(baseline).lower())
    output("changed", str(bool(changed_ids) or baseline).lower())
    output("changed_archives", ",".join(changed_ids))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
