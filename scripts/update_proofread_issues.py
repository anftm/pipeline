#!/usr/bin/env python3
"""Refresh the central proofreading review queue from linked pull requests."""

import json
import os
import re
import sys

try:
    from .submit_proofread import OWNER, TRACKER_REPOSITORY, api_request, full_repo_path, repo_path, response_or_fail
except ImportError:
    from submit_proofread import OWNER, TRACKER_REPOSITORY, api_request, full_repo_path, repo_path, response_or_fail


PRS_RE = re.compile(r"<!-- proofreading-prs:(\[.*?\]) -->")


def linked_pulls(body):
    match = PRS_RE.search(str(body or ""))
    if not match:
        return []
    try:
        pulls = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    return pulls if isinstance(pulls, list) else []


def pull_status(token, pull):
    repo = str(pull.get("repo") or "")
    number = pull.get("number")
    if not repo or not isinstance(number, int):
        raise RuntimeError("tracker issue contains an invalid pull request reference")
    status, data = api_request(token, "GET", f"{repo_path(repo)}/pulls/{number}")
    if status != 200:
        raise RuntimeError(f"cannot read {OWNER}/{repo}#{number}: HTTP {status}")
    return {
        "resolved": bool(data.get("merged")) or data.get("state") == "closed",
        "merged": bool(data.get("merged")),
    }


def refresh_issue(token, issue):
    pulls = linked_pulls(issue.get("body"))
    if not pulls:
        return False
    statuses = [pull_status(token, pull) for pull in pulls]
    body = str(issue.get("body") or "")
    for pull, status in zip(pulls, statuses):
        if status["resolved"]:
            body = body.replace(
                f"- [ ] [{pull['repo']}#{pull['number']}]",
                f"- [x] [{pull['repo']}#{pull['number']}]",
            )
    payload = {"body": body}
    if all(status["resolved"] for status in statuses):
        payload.update({"state": "closed", "state_reason": "completed"})
    response_or_fail(
        token, "PATCH", f"{full_repo_path(TRACKER_REPOSITORY)}/issues/{issue['number']}", (200,), payload,
    )
    return payload.get("state") == "closed"


def main():
    token = os.environ.get("TRACKER_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("TRACKER_TOKEN or GITHUB_TOKEN is required")
    refreshed = 0
    closed = 0
    issues_to_refresh = []
    for page in range(1, 101):
        status, issues = api_request(
            token, "GET",
            f"{full_repo_path(TRACKER_REPOSITORY)}/issues?state=open&labels=proofreading-review&per_page=100&page={page}",
        )
        if status != 200 or not isinstance(issues, list):
            raise RuntimeError(f"tracker issue listing failed with HTTP {status}")
        issues_to_refresh.extend(issue for issue in issues if linked_pulls(issue.get("body")))
        if len(issues) < 100:
            break
    else:
        raise RuntimeError("tracker issue listing exceeded pagination limit")
    for issue in issues_to_refresh:
        closed += int(refresh_issue(token, issue))
        refreshed += 1
    print(json.dumps({"refreshed": refreshed, "closed": closed}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
