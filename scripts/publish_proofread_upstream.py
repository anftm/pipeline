#!/usr/bin/env python3
"""Publish merged fork proofreading pull requests as batch pull requests."""

import base64
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


GITHUB_API = "https://api.github.com"
MIRROR_OWNER = os.environ.get("MIRROR_OWNER", "anftm")
UPSTREAM_OWNER = os.environ.get("UPSTREAM_OWNER", "banned-historical-archives")
REPOSITORY_PREFIX = os.environ.get("PROOFREAD_REPOSITORY_PREFIX", "banned-historical-archives")
STATE_PATH = os.environ.get("PROOFREAD_UPSTREAM_STATE", "state/proofread-upstream.json")
TRACKER_REPOSITORY = os.environ.get(
    "PROOFREAD_TRACKER_REPOSITORY", os.environ.get("GITHUB_REPOSITORY", f"{MIRROR_OWNER}/pipeline")
)
PRS_RE = re.compile(r"<!-- proofreading-prs:(\[.*?\]) -->")


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
        "branch": branch, "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha
    return response_or_fail(
        token, "PUT", f"{repo_path(owner, repo)}/contents/{urllib.parse.quote(path, safe='')}", (200, 201), payload,
    )


def delete_file(token, owner, repo, branch, path, sha, message):
    return response_or_fail(
        token, "DELETE", f"{repo_path(owner, repo)}/contents/{urllib.parse.quote(path, safe='')}", (200,),
        {"branch": branch, "message": message, "sha": sha},
    )


def delete_branch(token, owner, repo, branch):
    status, _data = api_request(token, "DELETE", ref_path(owner, repo, branch))
    if status not in {204, 404}:
        fail(f"cannot delete {owner}/{repo}:{branch}: HTTP {status}")


def pull_files(token, repo, number):
    paths = []
    for page in range(1, 4):
        status, data = api_request(
            token, "GET", f"{repo_path(MIRROR_OWNER, repo)}/pulls/{number}/files?per_page=100&page={page}",
        )
        if status != 200 or not isinstance(data, list):
            fail(f"cannot read files for {repo}#{number}: HTTP {status}")
        paths.extend(str(item.get("filename") or "") for item in data)
        if len(data) < 100:
            break
    if not paths or any(not path for path in paths) or len(paths) > 200:
        fail(f"{repo}#{number} has an invalid changed-file set")
    return paths


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
        if not pull.get("merged_at") or head.get("repo", {}).get("full_name") != f"{MIRROR_OWNER}/{repo}":
            continue
        head_ref = str(head.get("ref") or "")
        revert = re.match(r"^revert-([0-9]+)-", head_ref)
        if not head_ref.startswith("proofread/") and not revert:
            continue
        if pull.get("base", {}).get("ref") not in {"config", "ocr_patch"}:
            continue
        if revert:
            pull["reverts"] = int(revert.group(1))
        result.append(pull)
    return result


def merged_reverted_numbers(token, repo):
    reverted = set()
    for pull in list_closed_pulls(token, repo):
        if not pull.get("merged_at"):
            continue
        match = re.match(r"^revert-([0-9]+)-", str((pull.get("head") or {}).get("ref") or ""))
        if match:
            reverted.add(int(match.group(1)))
    return reverted


def filter_reverted_candidates(token, candidates, known=None):
    known = set(known or ())
    reverted_by_repo = {}
    kept = []
    skipped = []
    for pull in candidates:
        repo = pull["repo"]
        if repo not in reverted_by_repo:
            reverted_by_repo[repo] = merged_reverted_numbers(token, repo)
        if pull.get("reverts") and source_key(repo, pull["reverts"]) not in known:
            skipped.append(source_key(repo, pull["number"]))
        elif pull["number"] in reverted_by_repo[repo]:
            skipped.append(source_key(repo, pull["number"]))
        else:
            kept.append(pull)
    return kept, skipped


def pull_comment(token, repo, number, body):
    return response_or_fail(
        token, "POST", f"{repo_path(MIRROR_OWNER, repo)}/issues/{number}/comments", (201,), {"body": body}
    )


def open_upstream_pull(token, repo, branch, base):
    query = urllib.parse.urlencode({
        "state": "open", "head": f"{MIRROR_OWNER}:{branch}", "base": base, "per_page": 100,
    })
    status, data = api_request(token, "GET", f"{repo_path(UPSTREAM_OWNER, repo)}/pulls?{query}")
    if status != 200 or not isinstance(data, list):
        fail(f"cannot list upstream pull requests for {UPSTREAM_OWNER}/{repo}: HTTP {status}")
    return next((pull for pull in data if pull.get("base", {}).get("ref") == base), None)


def source_key(repo, number):
    return f"{repo}#{number}"


def upstream_path(repo, path):
    prefix = f"archives{repo.removeprefix(REPOSITORY_PREFIX)}/"
    return path[len(prefix):] if path.startswith(prefix) else path


def load_state():
    if not os.path.exists(STATE_PATH):
        return None
    with open(STATE_PATH, encoding="utf-8") as stream:
        state = json.load(stream)
    if not isinstance(state, dict) or state.get("version") != 1:
        fail("invalid proofreading upstream state")
    state.setdefault("baseline", [])
    state.setdefault("published", [])
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


def claim_repository(claim):
    repository = str(claim.get("upstream_repository") or "")
    if repository:
        return repository
    match = re.match(r"^https://github\.com/([^/]+/[^/]+)/pull/[0-9]+", str(claim.get("upstream_url") or ""))
    return match.group(1) if match else ""


def cleanup_legacy_claim(token, claim):
    repository = claim_repository(claim)
    number = claim.get("upstream_number")
    if repository != f"{MIRROR_OWNER}/pipeline" or not number:
        return False
    owner, repo = repository.split("/", 1)
    status, pull = api_request(token, "GET", f"{repo_path(owner, repo)}/pulls/{number}")
    if status == 404:
        return False
    if status != 200 or not isinstance(pull, dict):
        fail(f"cannot inspect legacy proofreading pull request {repository}#{number}: HTTP {status}")
    head = pull.get("head") or {}
    branch = str(head.get("ref") or "")
    if (
        "<!-- proofreading-upstream-batch -->" not in str(pull.get("body") or "")
        or (head.get("repo") or {}).get("full_name") != repository
        or not branch.startswith("proofread-upstream-")
    ):
        fail(f"refusing to clean unrecognized legacy pull request {repository}#{number}")
    if pull.get("state") == "open":
        response_or_fail(token, "PATCH", f"{repo_path(owner, repo)}/pulls/{number}", (200,), {"state": "closed"})
    status, _data = api_request(token, "DELETE", ref_path(owner, repo, branch))
    if status not in {204, 404}:
        fail(f"cannot delete legacy proofreading branch {repository}:{branch}: HTTP {status}")
    return True


def refresh_claims(token, state):
    state.setdefault("published", [])
    changed = False
    groups = {}
    cleaned_legacy = set()
    for key, claim in list(state["claimed"].items()):
        source_repo = key.rsplit("#", 1)[0]
        if claim_repository(claim) != f"{UPSTREAM_OWNER}/{source_repo}":
            legacy = (claim_repository(claim), claim.get("upstream_number"))
            if legacy not in cleaned_legacy:
                cleanup_legacy_claim(token, claim)
                cleaned_legacy.add(legacy)
            state["claimed"].pop(key, None)
            changed = True
            continue
        number = claim.get("upstream_number")
        if not number:
            state["claimed"].pop(key, None)
            changed = True
            continue
        groups.setdefault((source_repo, number), []).append(key)
    for (source_repo, number), keys in groups.items():
        status, data = api_request(token, "GET", f"{repo_path(UPSTREAM_OWNER, source_repo)}/pulls/{number}")
        if status == 200 and data.get("merged"):
            state["baseline"] = sorted(set(state["baseline"]) | set(keys))
            state["published"] = sorted(set(state["published"]) | set(keys))
            branch = str(state["claimed"].get(keys[0], {}).get("branch") or "") if keys else ""
            for key in keys:
                state["claimed"].pop(key, None)
            if branch.startswith("proofread-upstream-"):
                delete_branch(token, MIRROR_OWNER, source_repo, branch)
            changed = True
        elif status == 200 and data.get("state") == "open":
            continue
        elif status == 404 or (status == 200 and data.get("state") == "closed"):
            branch = str(state["claimed"].get(keys[0], {}).get("branch") or "") if keys else ""
            for key in keys:
                state["claimed"].pop(key, None)
            if branch.startswith("proofread-upstream-"):
                delete_branch(token, MIRROR_OWNER, source_repo, branch)
            changed = True
        else:
            fail(f"cannot refresh upstream pull request {UPSTREAM_OWNER}/{source_repo}#{number}: HTTP {status}")
    return changed


def pull_source_url(repo, pull):
    return pull.get("html_url") or f"https://github.com/{MIRROR_OWNER}/{repo}/pull/{pull['number']}"


def pull_references(body, repo):
    match = PRS_RE.search(str(body or ""))
    if not match:
        return []
    try:
        marker = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    return [int(item["number"]) for item in marker if str(item.get("repo")) == repo and item.get("number")]


def source_pull(token, repo, number):
    status, pull = api_request(token, "GET", f"{repo_path(MIRROR_OWNER, repo)}/pulls/{number}")
    if status != 200 or not isinstance(pull, dict):
        fail(f"cannot read source pull request {repo}#{number}: HTTP {status}")
    pull["repo"] = repo
    return pull


def combined_source_pulls(token, repo, existing, pulls):
    result = {pull["number"]: pull for pull in pulls}
    for number in pull_references((existing or {}).get("body"), repo):
        if number not in result:
            result[number] = source_pull(token, repo, number)
    return sorted(result.values(), key=lambda pull: pull.get("merged_at") or pull.get("number"))


def readable_details(body):
    if not body or "## 修改内容" not in body:
        return None
    lines = body.split("\n")
    start = next((index for index, line in enumerate(lines) if line.startswith("## ")), None)
    if start is None:
        return None
    end = len(lines)
    stop = ("## 审核方式", "## Pull Requests")
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## ") and lines[index].startswith(stop):
            end = index
            break
    return "\n".join(lines[start:end]).strip() or None


def tracker_path():
    owner, _, repo = TRACKER_REPOSITORY.partition("/")
    if not owner or not repo:
        fail(f"invalid tracker repository: {TRACKER_REPOSITORY}")
    return f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}"


def tracker_issue_body(token, repo, pull):
    for page in range(1, 21):
        status, issues = api_request(
            token, "GET", f"{tracker_path()}/issues?state=all&labels=proofreading-review&per_page=100&page={page}"
        )
        if status != 200 or not isinstance(issues, list):
            fail(f"cannot list tracker issues: HTTP {status}")
        for issue in issues:
            match = PRS_RE.search(str(issue.get("body") or ""))
            if not match:
                continue
            try:
                marker = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            if any(str(item.get("repo")) == repo and item.get("number") == pull["number"] for item in marker):
                return issue.get("body") or ""
        if len(issues) < 100:
            return None
    return None


def pull_change_details(token, repo, pull):
    section = readable_details(pull.get("body") or "")
    if section:
        return section
    try:
        tracker_body = tracker_issue_body(token, repo, pull)
    except Exception:
        return None
    return readable_details(tracker_body) if tracker_body else None


def split_batch_body(repo, base, pulls, details, limit=60000):
    marker = json.dumps([
        {"repo": repo, "number": pull["number"], "url": pull_source_url(repo, pull)}
        for pull in pulls
    ], ensure_ascii=False, separators=(",", ":"))
    links = "\n".join(
        f"- [x] [{repo}#{pull['number']}]({pull_source_url(repo, pull)})"
        for pull in pulls
    )
    core = [
        "<!-- proofreading-upstream-batch -->",
        f"<!-- proofreading-prs:{marker} -->",
        "此 PR 汇总已在 anftm fork 审核并合并的 BHA 校订。",
        "",
        "来源校订 PR：",
        links,
        "",
        f"目标仓库：`{UPSTREAM_OWNER}/{repo}`",
        f"目标分支：`{base}`",
        "",
        "请在目标仓库审核后使用 merge commit 合并；不要使用 squash 或 rebase，以便 fork 后续安全快进同步。",
    ]
    body = "\n".join(core)
    sections = []
    for pull, detail in zip(pulls, details):
        if not detail:
            continue
        sections.append(f"## 来源 PR：[{repo}#{pull['number']}]({pull_source_url(repo, pull)})\n\n{detail}")
    overflow = []
    for section in sections:
        if len(body) + len(section) + 2 <= limit:
            body += "\n\n" + section
        else:
            overflow.append(section)
    if overflow:
        body += "\n\n其余校订明细见下方评论。"
    return body, overflow


def chunk_lines(text, limit):
    return [text[index:index + limit] for index in range(0, len(text), limit)] or [""]


def post_batch_comments(token, owner, repo, number, sections):
    for section in sections:
        for comment in chunk_lines(section, 60000):
            response_or_fail(
                token, "POST", f"{repo_path(owner, repo)}/issues/{number}/comments", (201,), {"body": comment}
            )


def publish_group(token, repo, base, pulls):
    upstream_sha = branch_sha(token, UPSTREAM_OWNER, repo, base)
    mirror_sha = branch_sha(token, MIRROR_OWNER, repo, base)
    if not upstream_sha:
        fail(f"upstream branch does not exist: {UPSTREAM_OWNER}/{repo}/{base}")
    if not mirror_sha:
        fail(f"mirror branch does not exist: {MIRROR_OWNER}/{repo}/{base}")
    digest = hashlib.sha256(
        ",".join(source_key(repo, pull["number"]) for pull in pulls).encode("utf-8")
    ).hexdigest()[:10]
    branch = f"proofread-upstream-{repo}-{base}-{digest}"
    batch_sha = branch_sha(token, MIRROR_OWNER, repo, branch)
    if batch_sha is None:
        response_or_fail(
            token, "POST", f"{repo_path(MIRROR_OWNER, repo)}/git/refs", (201,),
            {"ref": f"refs/heads/{branch}", "sha": upstream_sha},
        )
    existing = open_upstream_pull(token, repo, branch, base)
    if existing:
        return existing, branch

    try:
        paths = {}
        for source_pull_item in pulls:
            for source_path in pull_files(token, repo, source_pull_item["number"]):
                target_path = upstream_path(repo, source_path)
                if not source_path or not target_path:
                    fail(f"{repo}#{source_pull_item['number']} has an invalid target path")
                previous = paths.get(target_path)
                if previous and previous[0] != source_path:
                    fail(f"multiple OCR patch paths map to {target_path}")
                source_revision = source_pull_item.get("merge_commit_sha")
                if not source_revision:
                    fail(f"{repo}#{source_pull_item['number']} is missing its merge commit")
                paths[target_path] = (source_path, source_revision)
        for target_path, (source_path, source_revision) in sorted(paths.items()):
            desired, _ = get_file(token, MIRROR_OWNER, repo, source_revision, source_path)
            current, current_sha = get_file(token, MIRROR_OWNER, repo, branch, target_path)
            if desired and desired != current:
                put_file(token, MIRROR_OWNER, repo, branch, target_path, desired, current_sha, f"Publish proofreading to {target_path}")
            elif not desired and current_sha:
                delete_file(token, MIRROR_OWNER, repo, branch, target_path, current_sha, f"Revert proofreading from {target_path}")

        details = [pull_change_details(token, repo, pull) for pull in pulls]
        body, overflow = split_batch_body(repo, base, pulls, details)
        payload = {
            "title": f"BHA proofreading batch: {repo}:{base}",
            "head": f"{MIRROR_OWNER}:{branch}", "base": base, "body": body,
        }
        pull = response_or_fail(
            token, "POST", f"{repo_path(UPSTREAM_OWNER, repo)}/pulls", (201,), payload,
        )
        if overflow:
            post_batch_comments(token, UPSTREAM_OWNER, repo, pull.get("number"), overflow)
        return pull, branch
    except Exception:
        try:
            if not open_upstream_pull(token, repo, branch, base):
                delete_branch(token, MIRROR_OWNER, repo, branch)
        except Exception:
            pass
        raise


def main():
    token = os.environ.get("GH_PAT", "")
    if not token:
        fail("GH_PAT is required")
    pulls = current_pulls(token)
    state = load_state()
    if state is None:
        if os.environ.get("PROOFREAD_BOOTSTRAP", "false").lower() != "true":
            fail("state is missing; run once with PROOFREAD_BOOTSTRAP=true to establish a baseline")
        state = {
            "version": 1,
            "baseline": sorted(source_key(p["repo"], p["number"]) for p in pulls),
            "published": [],
            "claimed": {},
        }
        save_state(state)
        print(json.dumps({"mode": "baseline", "count": len(pulls)}, ensure_ascii=False))
        return

    refresh_claims(token, state)
    known = set(state["baseline"]) | set(state["claimed"])
    published_or_claimed = set(state["published"]) | set(state["claimed"])
    candidates = [p for p in pulls if source_key(p["repo"], p["number"]) not in known]
    candidates, skipped = filter_reverted_candidates(token, candidates, published_or_claimed)
    if skipped:
        state["baseline"] = sorted(set(state["baseline"]) | set(skipped))
    groups = {}
    for pull in candidates:
        groups.setdefault((pull["repo"], pull["base"]["ref"]), []).append(pull)
    failures = []
    published = []
    for (repo, base), group in sorted(groups.items()):
        try:
            upstream, branch = publish_group(token, repo, base, group)
            if upstream is None:
                state["baseline"] = sorted(
                    set(state["baseline"]) | {source_key(repo, pull["number"]) for pull in group}
                )
                state["published"] = sorted(
                    set(state["published"]) | {source_key(repo, pull["number"]) for pull in group}
                )
                continue
            marker = f"<!-- proofreading-upstream:{repo}#{upstream['number']} -->"
            for pull in group:
                pull_comment(token, repo, pull["number"], marker)
                state["claimed"][source_key(repo, pull["number"])] = {
                    "upstream_repo": repo,
                    "upstream_repository": f"{UPSTREAM_OWNER}/{repo}",
                    "upstream_number": upstream["number"],
                    "upstream_url": upstream.get("html_url"),
                    "branch": branch,
                }
            published.append(upstream.get("html_url"))
        except Exception as exc:
            failures.append(f"{repo}/{base}: {exc}")
    save_state(state)
    print(json.dumps({"mode": "publish", "candidates": len(candidates), "skipped_reverted": skipped, "published": published, "failures": failures}, ensure_ascii=False))
    if failures:
        raise RuntimeError("; ".join(failures))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
