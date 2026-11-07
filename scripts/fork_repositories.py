#!/usr/bin/env python3
"""Create and safely update the anftm forks of the archive repositories.

New forks are created through GitHub's fork API, which copies the repository
branches. Existing forks are fast-forwarded on the data branches when
possible; divergent branches are reported instead of being overwritten.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


UPSTREAM_OWNER = os.environ.get("UPSTREAM_OWNER", "banned-historical-archives")
MIRROR_OWNER = os.environ.get("MIRROR_OWNER", "anftm")
REPO_START = int(os.environ.get("REPO_START", "0"))
REPO_END = int(os.environ.get("REPO_END", "31"))
GITHUB_API = "https://api.github.com"
DEFAULT_BRANCH = "main"
FORK_WAIT_SECONDS = 120
GENERATED_BRANCHES = {"parsed"}
LEGACY_DATA_BRANCHES = {"ocr_config", "parsed_article", "tags", "origin", "selected"}
SYNCED_BRANCHES = {"main", "config", "ocr_cache", "ocr_patch"} | LEGACY_DATA_BRANCHES
MANAGED_BRANCHES = GENERATED_BRANCHES | SYNCED_BRANCHES
TEMPORARY_BRANCH_RE = re.compile(r"^(?:proofread/[0-9a-f]{12}-(?:main|config|ocr_config|ocr_patch|origin)|revert-[0-9]+-.+)$")


def api_request(token: str, method: str, path: str, payload: dict | None = None):
    url = f"{GITHUB_API}{path}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "anftm-pipeline-fork-bot/1.0")
    request.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(detail).get("message", detail)
        except json.JSONDecodeError:
            pass
        return exc.code, {"message": detail}


def repo_path(owner: str, repo: str) -> str:
    return f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}"


def ref_path(owner: str, repo: str, branch: str) -> str:
    encoded_branch = urllib.parse.quote(branch, safe="")
    return f"{repo_path(owner, repo)}/git/ref/heads/{encoded_branch}"


def wait_for_fork(token: str, repo: str) -> bool:
    deadline = time.monotonic() + FORK_WAIT_SECONDS
    path = repo_path(MIRROR_OWNER, repo)
    while time.monotonic() < deadline:
        status, data = api_request(token, "GET", path)
        if status == 200 and data.get("default_branch"):
            return True
        time.sleep(5)
    return False


def ensure_fork(token: str, repo: str) -> str:
    upstream_path = repo_path(UPSTREAM_OWNER, repo)
    mirror_path = repo_path(MIRROR_OWNER, repo)
    status, data = api_request(token, "GET", mirror_path)
    if status == 200:
        if data.get("fork") and data.get("parent", {}).get("full_name") == f"{UPSTREAM_OWNER}/{repo}":
            print(f"[{repo}] fork exists")
        elif data.get("full_name") == f"{MIRROR_OWNER}/{repo}":
            raise RuntimeError(f"[{repo}] exists but is not the expected fork")
        else:
            raise RuntimeError(f"[{repo}] mirror lookup returned an unexpected repository")
    elif status == 404:
        status, data = api_request(token, "POST", f"{upstream_path}/forks", {"name": repo})
        if status not in (201, 202):
            raise RuntimeError(f"[{repo}] create fork failed: {data.get('message', data)}")
        print(f"[{repo}] fork creation requested")
        if not wait_for_fork(token, repo):
            raise RuntimeError(f"[{repo}] fork did not become available within {FORK_WAIT_SECONDS}s")
    else:
        raise RuntimeError(f"[{repo}] mirror lookup failed ({status}): {data.get('message', data)}")

    if not data.get("delete_branch_on_merge"):
        status, update = api_request(token, "PATCH", mirror_path, {"delete_branch_on_merge": True})
        if status == 200:
            print(f"[{repo}] enabled automatic merged-branch deletion")
        elif status in (403, 404, 422):
            # Repository settings are optional housekeeping and a fork may be
            # administered by the upstream owner. Do not fail archive syncing
            # (and its data branches) because this cosmetic setting is refused.
            print(f"[{repo}] branch cleanup setting not writable ({status}); continuing")
        else:
            raise RuntimeError(f"[{repo}] branch cleanup setting failed ({status}): {update.get('message', update)}")

    status, data = api_request(token, "GET", upstream_path)
    if status != 200:
        raise RuntimeError(f"[{repo}] upstream lookup failed ({status}): {data.get('message', data)}")
    return str(data.get("default_branch") or DEFAULT_BRANCH)


def branch_sha(token: str, owner: str, repo: str, branch: str) -> str | None:
    status, data = api_request(token, "GET", ref_path(owner, repo, branch))
    if status == 404:
        return None
    if status != 200:
        raise RuntimeError(f"[{repo}/{branch}] ref lookup failed ({status}): {data.get('message', data)}")
    return str(data.get("object", {}).get("sha") or "") or None


def list_branches(token: str, owner: str, repo: str) -> list[str]:
    branches: list[str] = []
    page = 1
    while True:
        status, data = api_request(
            token,
            "GET",
            f"{repo_path(owner, repo)}/branches?per_page=100&page={page}",
        )
        if status != 200 or not isinstance(data, list):
            detail = data.get("message", data) if isinstance(data, dict) else data
            raise RuntimeError(f"[{repo}] branch listing failed ({status}): {detail}")
        branches.extend(str(item.get("name")) for item in data if item.get("name"))
        if len(data) < 100:
            return branches
        page += 1


def sync_branch(token: str, repo: str, branch: str) -> None:
    upstream_sha = branch_sha(token, UPSTREAM_OWNER, repo, branch)
    if not upstream_sha:
        print(f"[{repo}/{branch}] upstream branch does not exist, skipped")
        return
    mirror_sha = branch_sha(token, MIRROR_OWNER, repo, branch)
    if mirror_sha == upstream_sha:
        print(f"[{repo}/{branch}] up to date")
        return
    if mirror_sha is None:
        status, data = api_request(
            token,
            "POST",
            f"{repo_path(MIRROR_OWNER, repo)}/git/refs",
            {"ref": f"refs/heads/{branch}", "sha": upstream_sha},
        )
        if status not in (200, 201):
            raise RuntimeError(f"[{repo}/{branch}] branch creation failed ({status}): {data.get('message', data)}")
        print(f"[{repo}/{branch}] created at upstream revision")
        return

    compare_path = (
        f"{repo_path(MIRROR_OWNER, repo)}/compare/"
        f"{urllib.parse.quote(upstream_sha, safe='')}...{urllib.parse.quote(mirror_sha, safe='')}"
    )
    compare_status, comparison = api_request(token, "GET", compare_path)
    if compare_status == 200:
        relation = comparison.get("status")
        if relation == "ahead":
            print(f"[{repo}/{branch}] fork is ahead of upstream, preserved")
            return
        if relation == "identical":
            print(f"[{repo}/{branch}] up to date")
            return
        if relation == "diverged":
            raise RuntimeError(f"[{repo}/{branch}] diverged; manual sync required")
        if relation != "behind":
            raise RuntimeError(f"[{repo}/{branch}] unexpected comparison status: {relation}")
    elif compare_status != 404:
        raise RuntimeError(
            f"[{repo}/{branch}] comparison failed ({compare_status}): "
            f"{comparison.get('message', comparison)}"
        )

    status, data = api_request(
        token,
        "PATCH",
        f"{repo_path(MIRROR_OWNER, repo)}/git/refs/heads/{urllib.parse.quote(branch, safe='')}",
        {"sha": upstream_sha, "force": False},
    )
    if status not in (200, 201):
        if status in (409, 422):
            raise RuntimeError(f"[{repo}/{branch}] diverged; manual sync required")
        raise RuntimeError(f"[{repo}/{branch}] update failed ({status}): {data.get('message', data)}")
    print(f"[{repo}/{branch}] fast-forwarded")


def delete_branch(token: str, repo: str, branch: str) -> None:
    status, data = api_request(token, "DELETE", ref_path(MIRROR_OWNER, repo, branch))
    if status not in (204, 404):
        raise RuntimeError(f"[{repo}/{branch}] deletion failed ({status}): {data.get('message', data)}")
    print(f"[{repo}/{branch}] deleted")


def branch_pulls(token: str, owner: str, repo: str, branch: str) -> list[dict]:
    query = urllib.parse.urlencode({"state": "all", "head": f"{owner}:{branch}", "per_page": 100})
    status, pulls = api_request(token, "GET", f"{repo_path(owner, repo)}/pulls?{query}")
    if status != 200 or not isinstance(pulls, list):
        raise RuntimeError(f"[{repo}/{branch}] pull lookup failed ({status})")
    return pulls


def clean_unmanaged_inherited_branches(
    token: str, repo: str, upstream: set[str], mirror: set[str], managed: set[str] | None = None,
) -> None:
    for branch in sorted((upstream & mirror) - (managed or MANAGED_BRANCHES)):
        pulls = branch_pulls(token, UPSTREAM_OWNER, repo, branch)
        if not pulls or any(pull.get("state") != "closed" for pull in pulls):
            print(f"[{repo}/{branch}] unmanaged inherited branch is not resolved, preserved")
            continue
        upstream_sha = branch_sha(token, UPSTREAM_OWNER, repo, branch)
        mirror_sha = branch_sha(token, MIRROR_OWNER, repo, branch)
        if upstream_sha != mirror_sha:
            print(f"[{repo}/{branch}] unmanaged inherited branch diverged, preserved")
            continue
        delete_branch(token, repo, branch)


def clean_resolved_temporary_branches(token: str, repo: str, upstream: set[str], mirror: set[str]) -> None:
    for branch in sorted(mirror - upstream):
        if not TEMPORARY_BRANCH_RE.fullmatch(branch):
            continue
        pulls = branch_pulls(token, MIRROR_OWNER, repo, branch)
        if pulls and all(pull.get("state") == "closed" for pull in pulls):
            delete_branch(token, repo, branch)


def inspect_fork(token: str, number: int) -> dict:
    """Compare one existing fork without creating, updating, or deleting refs."""
    if not 0 <= number <= 31:
        raise ValueError('archive number must be between 0 and 31')
    repo = f'banned-historical-archives{number}'
    repositories = {}
    for owner in (UPSTREAM_OWNER, MIRROR_OWNER):
        status, data = api_request(token, 'GET', repo_path(owner, repo))
        if status != 200 or not isinstance(data, dict) or data.get('full_name') != f'{owner}/{repo}':
            raise RuntimeError(f'[{repo}] {owner} repository lookup failed ({status})')
        repositories[owner] = data
    upstream, mirror = repositories[UPSTREAM_OWNER], repositories[MIRROR_OWNER]
    parent = mirror.get('parent')
    if (mirror.get('fork') is not True or not isinstance(parent, dict)
            or parent.get('full_name') != upstream['full_name']):
        raise ValueError('mirror is not the expected upstream fork')
    default = upstream.get('default_branch')
    if not isinstance(default, str) or not default:
        raise ValueError('upstream default branch is missing')
    upstream_branches = set(list_branches(token, UPSTREAM_OWNER, repo))
    mirror_branches = set(list_branches(token, MIRROR_OWNER, repo))
    names = sorted((upstream_branches | mirror_branches) & (MANAGED_BRANCHES | {default}))
    differences = []
    for name in names:
        a = branch_sha(token, UPSTREAM_OWNER, repo, name) if name in upstream_branches else None
        b = branch_sha(token, MIRROR_OWNER, repo, name) if name in mirror_branches else None
        if ((name in upstream_branches and not re.fullmatch(r'[0-9a-f]{40}', str(a or '')))
                or (name in mirror_branches and not re.fullmatch(r'[0-9a-f]{40}', str(b or '')))):
            raise RuntimeError('branch listing changed or contains an invalid revision during read-only inspection')
        differences.append({'branch': name, 'upstream': a, 'mirror': b,
                            'equal': a == b if a is not None and b is not None else False})
    return {'version': 1, 'kind': 'bha-fork-readonly-report', 'archive': number,
            'upstream': upstream['full_name'], 'mirror': mirror['full_name'],
            'default_branch': default, 'managed_branches': differences,
            'unmanaged_upstream_count': len(upstream_branches - (MANAGED_BRANCHES | {default})),
            'unmanaged_mirror_count': len(mirror_branches - (MANAGED_BRANCHES | {default})),
            'remote_writes': 0, 'synchronization_applied': False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inspect', type=int, help='read-only comparison for one archive (0-31)')
    parser.add_argument('--output', help='local JSON report for --inspect')
    args = parser.parse_args([] if argv is None else argv)
    if args.output and args.inspect is None:
        parser.error('--output requires --inspect')
    token = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("GH_PAT is required", file=sys.stderr)
        return 2
    if args.inspect is not None:
        report = inspect_fork(token, args.inspect)
        body = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
        if args.output:
            from pathlib import Path
            target = Path(args.output)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding='utf-8')
        print(body, end='')
        return 0

    failed = []
    for number in range(REPO_START, REPO_END + 1):
        repo = f"banned-historical-archives{number}"
        try:
            default_branch = ensure_fork(token, repo)
            upstream_branches = set(list_branches(token, UPSTREAM_OWNER, repo))
            mirror_branches = set(list_branches(token, MIRROR_OWNER, repo))
            managed_branches = MANAGED_BRANCHES | {default_branch}
            for data_branch in sorted(upstream_branches & (SYNCED_BRANCHES | {default_branch})):
                sync_branch(token, repo, data_branch)
            clean_unmanaged_inherited_branches(
                token, repo, upstream_branches, mirror_branches, managed_branches,
            )
            clean_resolved_temporary_branches(token, repo, upstream_branches, mirror_branches)
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            failed.append(repo)

    if failed:
        print("Failed repositories: " + ", ".join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
