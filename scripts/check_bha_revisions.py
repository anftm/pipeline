#!/usr/bin/env python3
"""Compare anftm parsed branch revisions and write a candidate state file."""

import http.client
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


OWNER = os.environ.get("BHA_ARCHIVE_OWNER", "anftm")
REPO_START = int(os.environ.get("REPO_START", "0"))
REPO_END = int(os.environ.get("REPO_END", "31"))
STATE_PATH = Path(os.environ.get("BHA_REVISION_STATE", "state/bha-parsed-revisions.json"))
CANDIDATE_PATH = Path(os.environ.get("BHA_REVISION_CANDIDATE", "/tmp/bha-parsed-revisions.json"))
COMMIT_CANDIDATE_PATH = Path(os.environ.get("BHA_COMMIT_CANDIDATE", "/tmp/bha-parsed-commits.json"))
GITHUB_OUTPUT = os.environ.get("GITHUB_OUTPUT")
GIT_PROPAGATION_TIMEOUT_SECONDS = int(os.environ.get("BHA_GIT_PROPAGATION_TIMEOUT", "600"))
GIT_PROPAGATION_POLL_SECONDS = int(os.environ.get("BHA_GIT_PROPAGATION_POLL", "10"))
REVISION_WORKERS = int(os.environ.get("BHA_REVISION_WORKERS", "8"))
GITHUB_API_ATTEMPTS = int(os.environ.get("BHA_GITHUB_API_ATTEMPTS", "4"))
GITHUB_API_RETRY_SECONDS = float(os.environ.get("BHA_GITHUB_API_RETRY_SECONDS", "1"))
RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}


def github_json(request: urllib.request.Request, label: str) -> dict:
    for attempt in range(GITHUB_API_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRYABLE_HTTP_STATUSES:
                raise RuntimeError(f"{label} failed with HTTP {exc.code}") from exc
            error: Exception = exc
        except (
            urllib.error.URLError,
            http.client.RemoteDisconnected,
            http.client.IncompleteRead,
            ConnectionError,
            TimeoutError,
            json.JSONDecodeError,
        ) as exc:
            error = exc
        if attempt + 1 == GITHUB_API_ATTEMPTS:
            raise RuntimeError(f"{label} failed after {GITHUB_API_ATTEMPTS} attempts: {error}") from error
        time.sleep(GITHUB_API_RETRY_SECONDS * (2 ** attempt))
    raise AssertionError("unreachable")


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
    data = github_json(request, f"{repo}/parsed lookup")
    value = str(data.get("object", {}).get("sha") or "")
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise RuntimeError(f"{repo}/parsed returned an invalid revision")
    return value


def tree_revision(token: str, archive_id: int, commit: str) -> str:
    repo = f"banned-historical-archives{archive_id}"
    owner = urllib.parse.quote(OWNER)
    url = f"https://api.github.com/repos/{owner}/{urllib.parse.quote(repo)}/git/commits/{commit}"
    request = urllib.request.Request(url)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "anftm-pipeline-bha-revisions/1.0")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    data = github_json(request, f"{repo}/parsed commit lookup")
    value = str(data.get("tree", {}).get("sha") or "")
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise RuntimeError(f"{repo}/parsed returned an invalid tree revision")
    return value


def archive_identity(token: str, archive_id: int) -> tuple[str, str, str]:
    commit = revision(token, archive_id)
    return str(archive_id), commit, tree_revision(token, archive_id, commit)


def git_revision(archive_id: int) -> str:
    repo = f"banned-historical-archives{archive_id}"
    result = subprocess.run(
        ["git", "ls-remote", f"https://github.com/{OWNER}/{repo}.git", "refs/heads/parsed"],
        check=True,
        capture_output=True,
        text=True,
    )
    value = result.stdout.split(maxsplit=1)[0] if result.stdout.strip() else ""
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise RuntimeError(f"{repo}/parsed git lookup returned an invalid revision")
    return value


def wait_for_git_revisions(expected: dict[str, str], archive_ids: list[str]) -> None:
    pending = list(archive_ids)
    deadline = time.monotonic() + GIT_PROPAGATION_TIMEOUT_SECONDS
    while pending:
        pending = [archive_id for archive_id in pending if git_revision(int(archive_id)) != expected[archive_id]]
        if not pending:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"parsed revisions did not propagate to Git transport: {', '.join(pending)}")
        print(f"waiting for parsed revisions to propagate: {','.join(pending)}")
        time.sleep(GIT_PROPAGATION_POLL_SECONDS)


def output(name: str, value: str) -> None:
    if GITHUB_OUTPUT:
        with open(GITHUB_OUTPUT, "a", encoding="utf-8") as target:
            target.write(f"{name}={value}\n")
    print(f"{name}={value}")


def main() -> None:
    token = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN", "")
    archive_ids = range(REPO_START, REPO_END + 1)
    with ThreadPoolExecutor(max_workers=REVISION_WORKERS) as executor:
        identities = list(executor.map(lambda archive_id: archive_identity(token, archive_id), archive_ids))
    commits = {archive_id: commit for archive_id, commit, _ in identities}
    current = {archive_id: tree for archive_id, _, tree in identities}
    try:
        previous = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        previous = None
    baseline = not isinstance(previous, dict)
    changed_ids = [key for key in current if isinstance(previous, dict) and previous.get(key) != current[key]]
    wait_for_git_revisions(commits, list(commits) if baseline else changed_ids)
    CANDIDATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CANDIDATE_PATH.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    COMMIT_CANDIDATE_PATH.write_text(json.dumps(commits, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output("baseline", str(baseline).lower())
    output("changed", str(bool(changed_ids) or baseline).lower())
    output("changed_archives", ",".join(changed_ids))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
