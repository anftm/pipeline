#!/usr/bin/env python3
"""Build parsed branches for anftm archives whose source branches changed."""

import base64
import json
import os
import re
import subprocess
import shutil
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
FORCE_REBUILD = os.environ.get("FORCE_REBUILD", "false").lower() == "true"
INPUT_BRANCHES = ("main", "config", "ocr_cache", "ocr_patch")
PARSED_BRANCH = "parsed"
PATCH_LAYOUT_VERSION = 2
LEGACY_IMAGE_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
LEGACY_FONT_TAG_RE = re.compile(r"</?font\b[^>]*>", re.IGNORECASE)


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
    required = (*INPUT_BRANCHES, PARSED_BRANCH)
    revisions = {}
    page = 1
    while True:
        status, data = api_request(
            token, "GET", f"{repo_path(owner, repo)}/branches?per_page=100&page={page}",
        )
        if status != 200 or not isinstance(data, list):
            raise RuntimeError(f"cannot list {owner}/{repo} branches: HTTP {status}")
        revisions.update({
            str(item.get("name")): str(item.get("commit", {}).get("sha") or "")
            for item in data if isinstance(item, dict)
        })
        if all(revisions.get(branch) for branch in required) or len(data) < 100:
            break
        page += 1
    missing = [branch for branch in required if not revisions.get(branch)]
    if missing:
        raise RuntimeError(f"{owner}/{repo} is missing branches: {', '.join(missing)}")
    return {branch: revisions[branch] for branch in required}


def ref_revision(token: str, owner: str, repo: str, branch: str) -> str:
    path = f"{repo_path(owner, repo)}/git/ref/heads/{urllib.parse.quote(branch, safe='')}"
    status, data = api_request(token, "GET", path)
    revision = str(data.get("object", {}).get("sha") or "") if isinstance(data, dict) else ""
    if status != 200 or not revision:
        raise RuntimeError(f"cannot read {owner}/{repo}:{branch}: HTTP {status}")
    return revision


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


def source_snapshot(revisions: dict[str, str], helper_revision: str) -> dict[str, str]:
    return {
        **{branch: revisions[branch] for branch in INPUT_BRANCHES},
        "helper": helper_revision,
        "patch_layout": str(PATCH_LAYOUT_VERSION),
    }


def needs_local_build(mirror: dict[str, str], upstream: dict[str, str]) -> bool:
    return any(mirror[branch] != upstream[branch] for branch in INPUT_BRANCHES)


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


def clean_environment() -> dict[str, str]:
    env = os.environ.copy()
    for key in ("GH_PAT", "GITHUB_TOKEN", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
        env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def push_environment(token: str, home: Path) -> dict[str, str]:
    env = git_environment(token)
    env["HOME"] = str(home)
    return env


def run(command: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=str(cwd) if cwd else None, env=env, check=True)


def run_output(command: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        command, cwd=str(cwd) if cwd else None, env=env,
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def clone_branch(repo_url: str, branch: str, target: Path, env: dict[str, str], expected_sha: str) -> None:
    run(["git", "clone", "--depth", "1", "--single-branch", "--branch", branch, repo_url, str(target)], env=env)
    if run_output(["git", "rev-parse", "HEAD"], cwd=target, env=env) != expected_sha:
        raise RuntimeError(f"{repo_url}:{branch} changed while preparing parsed data; retry the workflow")


def run_in_container(root: Path, cwd: Path, command: list[str]) -> None:
    run([
        "docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "--env", "HOME=/tmp",
        "--volume", f"{root}:{root}", "--workdir", str(cwd),
        "node:24", *command,
    ], env=clean_environment())


def prepare_helper(root: Path, env: dict[str, str], revision: str) -> Path:
    helper = root / "ocr_helper"
    clone_branch("https://github.com/banned-historical-archives/ocr_helper.git", "main", helper, env, revision)
    run_in_container(root, helper, ["npm", "install"])
    return helper


def sanitize_repository(repository: Path) -> None:
    git_dir = repository / ".git"
    if not git_dir.is_dir() or git_dir.is_symlink():
        raise RuntimeError("parsed repository metadata was replaced during generation")
    config = git_dir / "config"
    config.unlink(missing_ok=True)
    config.write_text("[core]\n\trepositoryformatversion = 0\n\tbare = false\n", encoding="utf-8")
    hooks = git_dir / "hooks"
    if hooks.exists():
        shutil.rmtree(hooks)
    hooks.mkdir()


def clean_legacy_image_markup(value):
    if isinstance(value, str):
        return LEGACY_FONT_TAG_RE.sub("", LEGACY_IMAGE_TAG_RE.sub("", value))
    if isinstance(value, list):
        return [clean_legacy_image_markup(item) for item in value]
    if isinstance(value, dict):
        return {key: clean_legacy_image_markup(item) for key, item in value.items()}
    return value


def clean_archive_20_parsed(parsed: Path, archive_id: int) -> None:
    if archive_id != 20:
        return
    for article_path in parsed.rglob("*.json"):
        article = json.loads(article_path.read_text(encoding="utf-8"))
        cleaned = clean_legacy_image_markup(article)
        article_path.write_text(
            json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )


def prepare_patch_input(source: Path, target: Path, archive_id: int) -> Path:
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(".git"))
    legacy = target / f"archives{archive_id}"
    if not legacy.exists():
        return target
    for path in legacy.iterdir():
        if not path.is_file() or path.suffix != ".ts":
            raise RuntimeError(f"unexpected legacy OCR patch path: {path}")
        destination = target / path.name
        if destination.exists() and destination.read_bytes() != path.read_bytes():
            raise RuntimeError(f"legacy OCR patch conflicts with root patch: {path.name}")
        if not destination.exists():
            shutil.copy2(path, destination)
    shutil.rmtree(legacy)
    return target


def parsed_tree_changed(parsed: Path, current_commit: str, env: dict[str, str]) -> bool:
    generated_tree = run_output(["git", "write-tree"], cwd=parsed, env=env)
    current_tree = run_output(["git", "rev-parse", f"{current_commit}^{{tree}}"], cwd=parsed, env=env)
    return generated_tree != current_tree


def build_archive(token: str, helper: Path, root: Path, archive_id: int, revisions: dict[str, str]) -> bool:
    repo = f"{REPOSITORY_PREFIX}{archive_id}"
    repo_url = f"https://github.com/{MIRROR_OWNER}/{repo}.git"
    archive_root = root / repo
    archive_root.mkdir()
    env = git_environment(token)
    paths = {}
    for branch in (*INPUT_BRANCHES, PARSED_BRANCH):
        path = archive_root / branch
        clone_branch(repo_url, branch, path, env, revisions[branch])
        paths[branch] = path

    parsed = paths[PARSED_BRANCH]
    patch_input = prepare_patch_input(paths["ocr_patch"], archive_root / "ocr_patch-input", archive_id)
    run(["git", "checkout", "--orphan", "parsed-build"], cwd=parsed, env=env)
    run(["git", "reset", "--hard"], cwd=parsed, env=env)
    run_in_container(root, helper, [
        "npm", "run", "build_parsed", "--",
        str(paths["config"]), str(paths["ocr_cache"]), str(patch_input),
        str(parsed), str(paths["main"]),
    ])
    clean_archive_20_parsed(parsed, archive_id)
    sanitize_repository(parsed)
    secure_home = archive_root / "push-home"
    secure_home.mkdir()
    safe_env = clean_environment()
    safe_env["HOME"] = str(secure_home)
    run(["git", "add", "-A"], cwd=parsed, env=safe_env)
    if not parsed_tree_changed(parsed, revisions[PARSED_BRANCH], safe_env):
        if ref_revision(token, MIRROR_OWNER, repo, PARSED_BRANCH) != revisions[PARSED_BRANCH]:
            raise RuntimeError(f"{MIRROR_OWNER}/{repo}:parsed changed during no-op generation; retry the workflow")
        return False
    run(["git", "config", "user.name", "github-actions[bot]"], cwd=parsed, env=safe_env)
    run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], cwd=parsed, env=safe_env)
    run(["git", "commit", "-m", "Rebuild parsed data from anftm corrections"], cwd=parsed, env=safe_env)
    run([
        "git", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=", "-c", "http.proxy=",
        "push", repo_url, "HEAD:parsed",
        f"--force-with-lease=refs/heads/parsed:{revisions[PARSED_BRANCH]}",
    ], cwd=parsed, env=push_environment(token, secure_home))
    return True


def sync_parsed(token: str, root: Path, archive_id: int, mirror: dict[str, str], upstream: dict[str, str]) -> None:
    repo = f"{REPOSITORY_PREFIX}{archive_id}"
    mirror_url = f"https://github.com/{MIRROR_OWNER}/{repo}.git"
    upstream_url = f"https://github.com/{UPSTREAM_OWNER}/{repo}.git"
    target = root / f"{repo}-parsed-sync"
    env = git_environment(token)
    clone_branch(mirror_url, PARSED_BRANCH, target, env, mirror[PARSED_BRANCH])
    run(["git", "fetch", "--depth", "1", upstream_url, PARSED_BRANCH], cwd=target, env=env)
    fetched = subprocess.run(
        ["git", "rev-parse", "FETCH_HEAD"], cwd=str(target), env=env, check=True, capture_output=True, text=True,
    ).stdout.strip()
    if fetched != upstream[PARSED_BRANCH]:
        raise RuntimeError(f"{UPSTREAM_OWNER}/{repo}:parsed changed while synchronizing; retry the workflow")
    secure_home = root / f"{repo}-sync-home"
    secure_home.mkdir()
    run([
        "git", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=", "-c", "http.proxy=",
        "push", mirror_url, "FETCH_HEAD:parsed",
        f"--force-with-lease=refs/heads/parsed:{mirror[PARSED_BRANCH]}",
    ], cwd=target, env=push_environment(token, secure_home))


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
    helper_revision = ref_revision(token, "banned-historical-archives", "ocr_helper", "main")
    selected = selected_archive_ids()
    current = {}
    for archive_id in selected:
        repo = f"{REPOSITORY_PREFIX}{archive_id}"
        current[archive_id] = branch_revisions(token, MIRROR_OWNER, repo)

    changed = [
        archive_id for archive_id in selected
        if FORCE_REBUILD or archives.get(str(archive_id)) != source_snapshot(current[archive_id], helper_revision)
    ]
    built = []
    synced = []
    if changed:
        with tempfile.TemporaryDirectory(prefix="bha-parsed-") as temporary:
            root = Path(temporary)
            helper = None
            for archive_id in changed:
                repo = f"{REPOSITORY_PREFIX}{archive_id}"
                upstream = branch_revisions(token, UPSTREAM_OWNER, repo)
                if FORCE_REBUILD or needs_local_build(current[archive_id], upstream):
                    helper = helper or prepare_helper(root, git_environment(token), helper_revision)
                    build_archive(token, helper, root, archive_id, current[archive_id])
                    built.append(archive_id)
                elif current[archive_id][PARSED_BRANCH] != upstream[PARSED_BRANCH]:
                    latest = branch_revisions(token, MIRROR_OWNER, repo)
                    if latest != current[archive_id]:
                        raise RuntimeError(f"{MIRROR_OWNER}/{repo} changed before parsed synchronization; retry the workflow")
                    sync_parsed(token, root, archive_id, current[archive_id], upstream)
                    synced.append(archive_id)
                archives[str(archive_id)] = source_snapshot(current[archive_id], helper_revision)

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
