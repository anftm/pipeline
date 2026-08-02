#!/usr/bin/env python3
"""Create human-reviewed revert PRs for an automatically merged correction."""

import json
import os
import re
import sys
from pathlib import Path

try:
    from .submit_proofread import (
        OWNER, REPOSITORY_PREFIX, TRACKER_REPOSITORY, api_request, full_repo_path, repo_path,
        response_or_fail,
    )
except ImportError:
    from submit_proofread import (
        OWNER, REPOSITORY_PREFIX, TRACKER_REPOSITORY, api_request, full_repo_path, repo_path,
        response_or_fail,
    )

AUTO_MARKER = "<!-- auto-merged:{correction_id} -->"
REVERT_MARKER = "<!-- proofreading-revert:{correction_id}:{repo}#{number} -->"
PR_RE = re.compile(rf"已合并 PR：\[({re.escape(REPOSITORY_PREFIX)}[0-9]+)#([0-9]+)\]\(([^)]+)\)")
COMMAND_RE = re.compile(r"^\s*/proofread-revert\s+([0-9a-f]{12})\s+CONFIRM\s*$", re.I)


def find_log(token):
    marker = "<!-- proofreading-auto-merge-log -->"
    for state in ("open", "closed"):
        for page in range(1, 101):
            status, issues = api_request(
                token, "GET",
                f"{full_repo_path(TRACKER_REPOSITORY)}/issues?state={state}&labels=proofreading-auto-merged&per_page=100&page={page}",
            )
            if status != 200 or not isinstance(issues, list):
                raise RuntimeError(f"auto-merge log lookup failed with HTTP {status}")
            for issue in issues:
                if marker in str(issue.get("body") or ""):
                    return issue
            if len(issues) < 100:
                break
    raise RuntimeError("auto-merge log Issue was not found")


def all_comments(token, issue_number):
    result = []
    for page in range(1, 101):
        status, comments = api_request(
            token, "GET",
            f"{full_repo_path(TRACKER_REPOSITORY)}/issues/{issue_number}/comments?per_page=100&page={page}",
        )
        if status != 200 or not isinstance(comments, list):
            raise RuntimeError(f"tracker comment lookup failed with HTTP {status}")
        result.extend(comments)
        if len(comments) < 100:
            return result
    return result


def authorized(token, actor):
    allowlist = {value.strip() for value in os.environ.get("PROOFREAD_REVERT_ACTORS", "").split(",") if value.strip()}
    if allowlist:
        return actor in allowlist
    status, data = api_request(
        token, "GET", f"{full_repo_path(TRACKER_REPOSITORY)}/collaborators/{actor}/permission",
    )
    return status == 200 and data.get("permission") in {"admin", "maintain", "push"}

def existing_revert(token, repo, number):
    status, pulls = api_request(token, "GET", f"{repo_path(repo)}/pulls?state=all&per_page=100")
    if status != 200 or not isinstance(pulls, list):
        return None
    needle = f"#{number}"
    return next((pull for pull in pulls if needle in str(pull.get("body") or "") and "revert" in str(pull.get("title") or "").lower()), None)


def correction_from_event():
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    event = json.loads(Path(event_path).read_text(encoding="utf-8")) if event_path else {}
    if event.get("issue") and event.get("comment"):
        actor = str(event.get("comment", {}).get("user", {}).get("login") or "")
        match = COMMAND_RE.fullmatch(str(event.get("comment", {}).get("body") or ""))
        if not match:
            return None, actor, int(event.get("issue", {}).get("number") or 0)
        return match.group(1), actor, int(event.get("issue", {}).get("number") or 0)
    correction_id = os.environ.get("PROOFREAD_CORRECTION_ID", "").strip()
    if correction_id and os.environ.get("PROOFREAD_CONFIRM") == "REVERT":
        return correction_id, os.environ.get("GITHUB_ACTOR", ""), 0
    raise RuntimeError("type REVERT or comment /proofread-revert <id> CONFIRM")


def main():
    archive_token = os.environ.get("GH_PAT", "")
    tracker_token = os.environ.get("TRACKER_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
    if not archive_token or not tracker_token:
        raise RuntimeError("GH_PAT and TRACKER_TOKEN are required")
    correction_id, actor, event_issue = correction_from_event()
    if not correction_id:
        return
    if not authorized(tracker_token, actor):
        raise RuntimeError(f"{actor or 'commenter'} is not authorized to revert proofreading")
    issue = find_log(tracker_token)
    if event_issue and event_issue != int(issue.get("number") or 0):
        raise RuntimeError("revert command must be posted on the auto-merge log Issue")
    comments = all_comments(tracker_token, issue["number"])
    marker = AUTO_MARKER.format(correction_id=correction_id)
    notification = next((comment.get("body", "") for comment in comments if marker in str(comment.get("body") or "")), None)
    if notification is None:
        raise RuntimeError("automatic merge notification was not found")
    pulls = PR_RE.findall(notification)
    if not pulls:
        raise RuntimeError("automatic merge notification contains no revertable PR")
    output = []
    for repo, raw_number, url in pulls:
        number = int(raw_number)
        item_marker = REVERT_MARKER.format(correction_id=correction_id, repo=repo, number=number)
        if any(item_marker in str(comment.get("body") or "") for comment in comments):
            output.append(f"{repo}#{number}: already requested")
            continue
        status, pull = api_request(archive_token, "GET", f"{repo_path(repo)}/pulls/{number}")
        if status != 200 or not pull.get("merged"):
            raise RuntimeError(f"{repo}#{number} is no longer a merged PR")
        revert = existing_revert(archive_token, repo, number)
        status = 201 if revert else 0
        if not revert:
            status, revert = api_request(archive_token, "POST", f"{repo_path(repo)}/pulls/{number}/revert")
        if status not in (201, 202):
            body = f"{item_marker}\n撤回 PR 创建失败：{repo}#{number} HTTP {status}。请人工处理。"
            response_or_fail(tracker_token, "POST", f"{full_repo_path(TRACKER_REPOSITORY)}/issues/{issue['number']}/comments", (201,), {"body": body})
            continue
        body = (
            f"{item_marker}\n已为原 PR [{repo}#{number}]({url}) 创建撤回 PR "
            f"[{revert.get('html_url', 'unknown')}]({revert.get('html_url', 'unknown')})。"
            "撤回 PR 需要人工审核和合并。"
        )
        response_or_fail(tracker_token, "POST", f"{full_repo_path(TRACKER_REPOSITORY)}/issues/{issue['number']}/comments", (201,), {"body": body})
        output.append(f"{repo}#{number}: revert PR created")
    print(json.dumps({"correction_id": correction_id, "results": output}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
