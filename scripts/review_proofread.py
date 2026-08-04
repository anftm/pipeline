#!/usr/bin/env python3
"""Apply /approve and /reject review commands on proofreading tracker issues."""

import json
import os
import re
import sys
from pathlib import Path

try:
    from .submit_proofread import (
        OWNER, TRACKER_REPOSITORY, api_request, full_repo_path, merge_pull, repo_path, response_or_fail,
    )
except ImportError:
    from submit_proofread import (
        OWNER, TRACKER_REPOSITORY, api_request, full_repo_path, merge_pull, repo_path, response_or_fail,
    )

PRS_RE = re.compile(r"<!-- proofreading-prs:(\[.*?\]) -->")
APPROVE_RE = re.compile(r"^\s*/approve\s*$", re.I)
REJECT_RE = re.compile(r"^\s*/reject(?:\s+(.*))?\s*$", re.I)


def authorized(token, actor):
    allowlist = {value.strip() for value in os.environ.get("PROOFREAD_REVIEW_ACTORS", "").split(",") if value.strip()}
    if allowlist:
        return actor in allowlist
    status, data = api_request(
        token, "GET", f"{full_repo_path(TRACKER_REPOSITORY)}/collaborators/{actor}/permission",
    )
    return status == 200 and data.get("permission") in {"admin", "maintain", "push"}


def linked_pulls(body):
    match = PRS_RE.search(str(body or ""))
    if not match:
        return []
    try:
        pulls = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    return pulls if isinstance(pulls, list) else []


def pull_data(token, pull):
    repo = str(pull.get("repo") or "")
    number = pull.get("number")
    if not repo or not isinstance(number, int):
        raise RuntimeError("tracker issue contains an invalid pull request reference")
    status, data = api_request(token, "GET", f"{repo_path(repo)}/pulls/{number}")
    if status != 200:
        raise RuntimeError(f"cannot read {OWNER}/{repo}#{number}: HTTP {status}")
    return data


def close_pull(token, repo, number):
    status, _data = api_request(token, "PATCH", f"{repo_path(repo)}/pulls/{number}", {"state": "closed"})
    if status != 200:
        raise RuntimeError(f"{repo}#{number} close failed with HTTP {status}")
    return True


def main():
    archive_token = os.environ.get("GH_PAT", "")
    tracker_token = os.environ.get("TRACKER_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
    if not archive_token or not tracker_token:
        raise RuntimeError("GH_PAT and TRACKER_TOKEN are required")
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    event = json.loads(Path(event_path).read_text(encoding="utf-8")) if event_path else {}
    issue = event.get("issue") or {}
    comment = event.get("comment") or {}
    actor = str(comment.get("user", {}).get("login") or "")
    body = str(comment.get("body") or "")
    approve = APPROVE_RE.fullmatch(body)
    reject = REJECT_RE.fullmatch(body)
    if not approve and not reject:
        return
    if not authorized(tracker_token, actor):
        raise RuntimeError(f"{actor or 'commenter'} is not authorized to review proofreading")
    pulls = linked_pulls(issue.get("body"))
    if not pulls:
        raise RuntimeError("review issue contains no linked pull requests")
    issue_number = int(issue.get("number") or 0)
    if not issue_number:
        raise RuntimeError("review issue number is missing")
    results = []
    all_resolved = True
    for pull in pulls:
        repo = str(pull.get("repo") or "")
        number = pull.get("number")
        data = pull_data(archive_token, pull)
        if not isinstance(number, int):
            raise RuntimeError("tracker issue contains an invalid pull request reference")
        if data.get("merged") or data.get("state") == "closed":
            results.append(f"{repo}#{number}: already resolved")
            continue
        if approve:
            if merge_pull(archive_token, repo, pull):
                results.append(f"{repo}#{number}: merged")
            else:
                results.append(f"{repo}#{number}: not mergeable")
                all_resolved = False
        else:
            close_pull(archive_token, repo, number)
            results.append(f"{repo}#{number}: closed")
    summary = "已同意并合并/关闭相关 PR" if approve else "已拒绝本次校订"
    reason = reject.group(1) if reject else ""
    lines = [f"<!-- review-command:{actor} -->", f"**{summary}**", ""]
    lines.extend(f"- {result}" for result in results)
    if reason:
        lines.extend(["", f"原因：{reason}"])
    response_or_fail(
        tracker_token, "POST", f"{full_repo_path(TRACKER_REPOSITORY)}/issues/{issue_number}/comments", (201,),
        {"body": "\n".join(lines)},
    )
    if all_resolved:
        response_or_fail(
            tracker_token, "PATCH", f"{full_repo_path(TRACKER_REPOSITORY)}/issues/{issue_number}", (200,),
            {"state": "closed", "state_reason": "completed"},
        )
    print(json.dumps({
        "command": "approve" if approve else "reject", "actor": actor,
        "results": results, "issue_closed": all_resolved,
    }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
