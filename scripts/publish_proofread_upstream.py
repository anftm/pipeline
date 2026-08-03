#!/usr/bin/env python3
"""Publish merged fork proofreading pull requests as upstream pull requests."""

import base64
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


GITHUB_API = "https://api.github.com"
UPSTREAM_OWNER = os.environ.get("UPSTREAM_OWNER", "banned-historical-archives")
MIRROR_OWNER = os.environ.get("MIRROR_OWNER", "anftm")
REPOSITORY_PREFIX = os.environ.get("PROOFREAD_REPOSITORY_PREFIX", "banned-historical-archives")
STATE_PATH = os.environ.get("PROOFREAD_UPSTREAM_STATE", "state/proofread-upstream.json")


def api_request(token, method, path, payload=None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(f"{GITHUB_API}{path}", data=body, method=method)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "anftm-pipeline-proofread-upstream/1.0")
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


def fail(message):
    raise RuntimeError(message)


def response_or_fail(token, method, path, expected, payload=None):
    status, data = api_request(token, method, path, payload)
    if status not in expected:
        detail = data.get("message", data) if isinstance(data, dict) else data
        fail(f"GitHub {method} {path} failed ({status}): {detail}")
    return data


def repo_path(owner, repo):
    return f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}"


def ref_path(owner, repo, branch):
    return f"{repo_path(owner, repo)}/git/ref/heads/{urllib.parse.quote(branch, safe='')}"


def branch_sha(token, owner, repo, branch):
    status, data = api_request(token, "GET", ref_path(owner, repo, branch))
    if status == 404:
        return None
    if status != 200:
        fail(f"cannot read {owner}/{repo}/{branch}: {data}")
    return data.get("object", {}).get("sha")


def get_file(token, owner, repo, ref, path):
    encoded = urllib.parse.quote(path, safe="")
    status, data = api_request(
        token, "GET", f"{repo_path(owner, repo)}/contents/{encoded}?ref={urllib.parse.quote(ref, safe='')}"
    )
    if status == 404:
        return "", None
    if status != 200 or data.get("encoding") != "base64" or not data.get("content"):
        fail(f"cannot read {owner}/{repo}/{ref}/{path}: {data}")
    return base64.b64decode(data["content"].replace("\n", "")).decode("utf-8"), data.get("sha")


def put_file(token, owner, repo, branch, path, content, sha, message):
    payload = {
        "branch": branch,
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha
    return response_or_fail(
        token, "PUT", f"{repo_path(owner, repo)}/contents/{urllib.parse.quote(path, safe='')}", (200, 201), payload
    )


def delete_branch(token, owner, repo, branch):
    response_or_fail(token, "DELETE", ref_path(owner, repo, branch), (204,))


def list_closed_pulls(token, repo):
    result = []
    for page in range(1, 21):
        status, data = api_request(
            token, "GET", f"{repo_path(MIRROR_OWNER, repo)}/pulls?state=closed&per_page=100&page={page}"
        )
        if status != 200 or not isinstance(data, list):
            fail(f"cannot list closed pull requests for {repo}: HTTP {status}")
        result.extend(data)
        if len(data) < 100:
            return result
    fail(f"closed pull request listing exceeded pagination limit for {repo}")


def proofreading_pulls(token, repo):
    result = []
    for pull in list_closed_pulls(token, repo):
        head = pull.get("head") or {}
        if not pull.get("merged") or head.get("repo", {}).get("full_name") != f"{MIRROR_OWNER}/{repo}":
            continue
        if not str(head.get("ref") or "").startswith("proofread/"):
            continue
        if pull.get("base", {}).get("ref") not in {"config", "ocr_patch"}:
            continue
        result.append(pull)
    return result


def pull_files(token, repo, number):
    status, data = api_request(token, "GET", f"{repo_path(MIRROR_OWNER, repo)}/pulls/{number}/files?per_page=100")
    if status != 200 or not isinstance(data, list):
        fail(f"cannot read files for {repo}#{number}: HTTP {status}")
    if len(data) != 1 or data[0].get("status") not in {"added", "modified"}:
        fail(f"{repo}#{number} must contain exactly one added or modified file")
    return data[0].get("filename")


def pull_comment(token, repo, number, body):
    return response_or_fail(
        token, "POST", f"{repo_path(MIRROR_OWNER, repo)}/issues/{number}/comments", (201,), {"body": body}
    )


def open_upstream_pull(token, repo, branch, base):
    query = urllib.parse.urlencode({"state": "open", "head": f"{MIRROR_OWNER}:{branch}", "per_page": 100})
    status, data = api_request(token, "GET", f"{repo_path(UPSTREAM_OWNER, repo)}/pulls?{query}")
    if status != 200 or not isinstance(data, list):
        fail(f"cannot list upstream pull requests for {repo}: HTTP {status}")
    return next((pull for pull in data if pull.get("base", {}).get("ref") == base), None)


def source_key(repo, number):
    return f"{repo}#{number}"


def load_state():
    if not os.path.exists(STATE_PATH):
        return None
    with open(STATE_PATH, encoding="utf-8") as stream:
        state = json.load(stream)
    if not isinstance(state, dict) or state.get("version") != 1:
        fail("invalid proofreading upstream state")
    state.setdefault("baseline", [])
    state.setdefault("claimed", {})
    return state


def save_state(state):
    parent = os.path.dirname(STATE_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    temporary = f"{STATE_PATH}.tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    os.replace(temporary, STATE_PATH)


def current_pulls(token):
    result = []
    for archive_id in range(32):
        repo = f"{REPOSITORY_PREFIX}{archive_id}"
        for pull in proofreading_pulls(token, repo):
            pull["repo"] = repo
            result.append(pull)
    return result


def refresh_claims(token, state):
    changed = False
    for key, claim in list(state["claimed"].items()):
        repo = claim.get("upstream_repo")
        number = claim.get("upstream_number")
        if not repo or not number:
            state["claimed"].pop(key, None)
            changed = True
            continue
        status, data = api_request(token, "GET", f"{repo_path(UPSTREAM_OWNER, repo)}/pulls/{number}")
        if status == 200 and (data.get("state") == "open" or data.get("merged")):
            continue
        if status == 404 or (status == 200 and data.get("state") == "closed"):
            state["claimed"].pop(key, None)
            changed = True
    return changed


def publish_group(token, repo, base, pulls):
    upstream_sha = branch_sha(token, UPSTREAM_OWNER, repo, base)
    if not upstream_sha:
        fail(f"upstream branch does not exist: {repo}/{base}")
    digest = hashlib.sha256(
        ",".join(source_key(repo, pull["number"]) for pull in pulls).encode("utf-8")
    ).hexdigest()[:10]
    branch = f"proofread-upstream-{base}-{digest}"
    mirror_sha = branch_sha(token, MIRROR_OWNER, repo, branch)
    if mirror_sha is None:
        response_or_fail(
            token, "POST", f"{repo_path(MIRROR_OWNER, repo)}/git/refs", (201,),
            {"ref": f"refs/heads/{branch}", "sha": upstream_sha},
        )
    existing = open_upstream_pull(token, repo, branch, base)
    if existing:
        return existing, branch
    if mirror_sha is not None and mirror_sha != upstream_sha:
        fail(f"batch branch already exists with an unexpected revision: {repo}/{branch}")

    try:
        for pull in sorted(pulls, key=lambda item: item.get("merged_at") or item.get("number")):
            path = pull_files(token, repo, pull["number"])
            if not path:
                fail(f"{repo}#{pull['number']} has an invalid target file")
            base_ref = pull.get("base", {}).get("sha")
            merge_ref = pull.get("merge_commit_sha")
            if not base_ref or not merge_ref:
                fail(f"{repo}#{pull['number']} is missing base or merge revision")
            source_base, _ = get_file(token, MIRROR_OWNER, repo, base_ref, path)
            batch_content, batch_sha = get_file(token, MIRROR_OWNER, repo, branch, path)
            if batch_content != source_base:
                fail(f"conflict while applying {repo}#{pull['number']} to {base}/{path}")
            desired, _ = get_file(token, MIRROR_OWNER, repo, merge_ref, path)
            if not desired:
                fail(f"merged pull {repo}#{pull['number']} has no readable file content")
            put_file(token, MIRROR_OWNER, repo, branch, path, desired, batch_sha, f"Apply proofreading {repo}#{pull['number']}")
    except Exception:
        try:
            if not open_upstream_pull(token, repo, branch, base):
                delete_branch(token, MIRROR_OWNER, repo, branch)
        except Exception:
            pass
        raise

    sources = ", ".join(f"{repo}#{pull['number']}" for pull in pulls)
    body = "\n".join([
        "<!-- proofreading-upstream-batch -->",
        "此 PR 汇总已在 anftm fork 审核并合并的 BHA 校订。",
        "",
        f"来源校订 PR：{sources}",
        "",
        "请在原仓库审核后合并。",
    ])
    pull = response_or_fail(
        token, "POST", f"{repo_path(UPSTREAM_OWNER, repo)}/pulls", (201,),
        {"title": f"BHA proofreading batch: {base}", "head": f"{MIRROR_OWNER}:{branch}", "base": base, "body": body},
    )
    return pull, branch


def main():
    token = os.environ.get("GH_PAT", "")
    if not token:
        fail("GH_PAT is required")
    pulls = current_pulls(token)
    state = load_state()
    if state is None:
        if os.environ.get("PROOFREAD_BOOTSTRAP", "false").lower() != "true":
            fail("state is missing; run once with PROOFREAD_BOOTSTRAP=true to establish a baseline")
        state = {"version": 1, "baseline": sorted(source_key(p["repo"], p["number"]) for p in pulls), "claimed": {}}
        save_state(state)
        print(json.dumps({"mode": "baseline", "count": len(pulls)}, ensure_ascii=False))
        return

    refresh_claims(token, state)
    known = set(state["baseline"]) | set(state["claimed"])
    candidates = [p for p in pulls if source_key(p["repo"], p["number"]) not in known]
    groups = {}
    for pull in candidates:
        groups.setdefault((pull["repo"], pull["base"]["ref"]), []).append(pull)
    failures = []
    published = []
    for (repo, base), group in sorted(groups.items()):
        try:
            upstream, branch = publish_group(token, repo, base, group)
            marker = f"<!-- proofreading-upstream:{repo}#{upstream['number']} -->"
            for pull in group:
                pull_comment(token, repo, pull["number"], marker)
                state["claimed"][source_key(repo, pull["number"])] = {
                    "upstream_repo": repo,
                    "upstream_number": upstream["number"],
                    "upstream_url": upstream.get("html_url"),
                    "branch": branch,
                }
            published.append(upstream.get("html_url"))
        except Exception as exc:
            failures.append(f"{repo}/{base}: {exc}")
    save_state(state)
    print(json.dumps({"mode": "publish", "candidates": len(candidates), "published": published, "failures": failures}, ensure_ascii=False))
    if failures:
        raise RuntimeError("; ".join(failures))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
