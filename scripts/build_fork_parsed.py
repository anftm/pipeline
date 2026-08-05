#!/usr/bin/env python3
"""Build parsed branches for anftm archives whose source branches changed."""

import base64
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


GITHUB_API = "https://api.github.com"
MIRROR_OWNER = os.environ.get("MIRROR_OWNER", "anftm")
UPSTREAM_OWNER = os.environ.get("UPSTREAM_OWNER", "banned-historical-archives")
REPOSITORY_PREFIX = os.environ.get("BHA_REPOSITORY_PREFIX", "banned-historical-archives")
STATE_PATH = Path(os.environ.get("BHA_PARSED_INPUT_STATE", "state/bha-parsed-inputs.json"))
CANDIDATE_PATH = Path(os.environ.get("BHA_PARSED_INPUT_CANDIDATE", "/tmp/bha-parsed-inputs.json"))
ARCHIVE_ID = os.environ.get("ARCHIVE_ID", "all")
INPUT_BRANCHES = ("main", "config", "ocr_cache", "ocr_patch")
PARSED_BRANCH = "parsed"


def api_request(token: str, method: str, path: str, payload: dict | None = None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(f"{GITHUB_API}{path}", data=body, method=method)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "anftm-pipeline-build-parsed/1.0")
    request.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw)
        except json.JSONDecodeError:
            detail = {"message": raw}
        return exc.code, detail


def repo_path(owner: str, repo: str) -> str:
    return f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}"


def branch_revisions(token: str, owner: str, repo: str) -> dict[str, str]:
    status, data = api_request(token, "GET", f"{repo_path(owner, repo)}/branches?per_page=100")
    if status != 200 or not isinstance(data, list):
        raise RuntimeError(f"cannot list {owner}/{repo} branches: HTTP {status}")
    revisions = {
        str(item.get("name")): str(item.get("commit", {}).get("sha") or "")
        for item in data if isinstance(item, dict)
    }
    required = (*INPUT_BRANCHES, PARSED_BRANCH)
    missing = [branch for branch in required if not revisions.get(branch)]
    if missing:
        raise RuntimeError(f"{owner}/{repo} is missing branches: {', '.join(missing)}")
    return {branch: revisions[branch] for branch in required}


def selected_archive_ids() -> list[int]:
    if ARCHIVE_ID == "all":
        return list(range(32))
    try:
        archive_id = int(ARCHIVE_ID)
    except ValueError as exc:
        raise RuntimeError("ARCHIVE_ID must be all or an integer from 0 through 31") from exc
    if archive_id < 0 or archive_id > 31:
        raise RuntimeError("ARCHIVE_ID must be all or an integer from 0 through 31")
    return [archive_id]


def load_state() -> dict:
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "archives": {}}
    if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("archives"), dict):
        raise RuntimeError("invalid BHA parsed input state")
    return state


def source_snapshot(revisions: dict[str, str]) -> dict[str, str]:
    return {branch: revisions[branch] for branch in INPUT_BRANCHES}


def needs_local_build(mirror: dict[str, str], upstream: dict[str, str]) -> bool:
    return any(mirror[branch] != upstream[branch] for branch in INPUT_BRANCHES)


def sync_parsed(token: str, repo: str, mirror_sha: str, upstream_sha: str) -> bool:
    if mirror_sha == upstream_sha:
        return False
    path = f"{repo_path(MIRROR_OWNER, repo)}/git/refs/heads/{urllib.parse.quote(PARSED_BRANCH, safe='')}"
    status, _data = api_request(token, "PATCH", path, {"sha": upstream_sha, "force": True})
    if status != 200:
        raise RuntimeError(f"cannot sync {MIRROR_OWNER}/{repo}:{PARSED_BRANCH}: HTTP {status}")
    return True


def git_environment(token: str) -> dict[str, str]:
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = os.environ.copy()
    env.update({
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {encoded}",
        "GIT_TERMINAL_PROMPT": "0",
    })
    return env


def run(command: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=str(cwd) if cwd else None, env=env, check=True)


def clone_branch(repo_url: str, branch: str, target: Path, env: dict[str, str]) -> None:
    run(["git", "clone", "--depth", "1", "--single-branch", "--branch", branch, repo_url, str(target)], env=env)


def prepare_helper(root: Path, env: dict[str, str]) -> Path:
    helper = root / "ocr_helper"
    clone_branch("https://github.com/banned-historical-archives/ocr_helper.git", "main", helper, env)
    run(["npm", "install"], cwd=helper, env=env)
    return helper


def build_archive(token: str, helper: Path, root: Path, archive_id: int) -> None:
    repo = f"{REPOSITORY_PREFIX}{archive_id}"
    repo_url = f"https://github.com/{MIRROR_OWNER}/{repo}.git"
    archive_root = root / repo
    archive_root.mkdir()
    env = git_environment(token)
    paths = {}
    for branch in (*INPUT_BRANCHES, PARSED_BRANCH):
        path = archive_root / branch
        clone_branch(repo_url, branch, path, env)
        paths[branch] = path

    parsed = paths[PARSED_BRANCH]
    run(["git", "checkout", "--orphan", "parsed-build"], cwd=parsed, env=env)
    run(["git", "reset", "--hard"], cwd=parsed, env=env)
    run([
        "npm", "run", "build_parsed", "--",
        str(paths["config"]), str(paths["ocr_cache"]), str(paths["ocr_patch"]),
        str(parsed), str(paths["main"]),
    ], cwd=helper, env=env)
    run(["git", "config", "user.name", "github-actions[bot]"], cwd=parsed, env=env)
    run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], cwd=parsed, env=env)
    run(["git", "add", "-A"], cwd=parsed, env=env)
    run(["git", "commit", "-m", "Rebuild parsed data from anftm corrections"], cwd=parsed, env=env)
    run(["git", "push", "origin", "HEAD:parsed", "--force"], cwd=parsed, env=env)


def output(name: str, value: str) -> None:
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as target:
            target.write(f"{name}={value}\n")
    print(f"{name}={value}")


def main() -> None:
    token = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("GH_PAT is required")
    state = load_state()
    archives = dict(state["archives"])
    selected = selected_archive_ids()
    current = {}
    for archive_id in selected:
        repo = f"{REPOSITORY_PREFIX}{archive_id}"
        current[archive_id] = branch_revisions(token, MIRROR_OWNER, repo)

    changed = [archive_id for archive_id in selected if archives.get(str(archive_id)) != source_snapshot(current[archive_id])]
    built = []
    synced = []
    if changed:
        with tempfile.TemporaryDirectory(prefix="bha-parsed-") as temporary:
            root = Path(temporary)
            helper = None
            for archive_id in changed:
                repo = f"{REPOSITORY_PREFIX}{archive_id}"
                upstream = branch_revisions(token, UPSTREAM_OWNER, repo)
                if needs_local_build(current[archive_id], upstream):
                    helper = helper or prepare_helper(root, git_environment(token))
                    build_archive(token, helper, root, archive_id)
                    built.append(archive_id)
                elif sync_parsed(token, repo, current[archive_id][PARSED_BRANCH], upstream[PARSED_BRANCH]):
                    synced.append(archive_id)
                archives[str(archive_id)] = source_snapshot(current[archive_id])

    candidate = {"version": 1, "archives": archives}
    CANDIDATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CANDIDATE_PATH.write_text(json.dumps(candidate, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output("state_changed", str(candidate != state).lower())
    output("changed_archives", ",".join(map(str, changed)))
    output("built_archives", ",".join(map(str, built)))
    output("synced_archives", ",".join(map(str, synced)))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
