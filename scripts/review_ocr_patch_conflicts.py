#!/usr/bin/env python3
"""Apply per-article keep/drop commands on OCR baseline conflict issues."""

import base64
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

try:
    from .report_ocr_patch_conflicts import (
        CONFLICT_RE, DECISIONS_RE, decode_marker, encode_marker, review_status,
    )
    from .review_proofread import authorized
    from .submit_proofread import TRACKER_REPOSITORY, api_request, full_repo_path, response_or_fail
except ImportError:
    from report_ocr_patch_conflicts import (
        CONFLICT_RE, DECISIONS_RE, decode_marker, encode_marker, review_status,
    )
    from review_proofread import authorized
    from submit_proofread import TRACKER_REPOSITORY, api_request, full_repo_path, response_or_fail


COMMAND_RE = re.compile(r"^\s*/ocr-(keep|drop)\s+(all|[0-9]+(?:[\s,]+[0-9]+)*)\s*$", re.I)
REVIEW_BLOCK_RE = re.compile(r"<!-- ocr-review-start -->.*?<!-- ocr-review-end -->", re.DOTALL)
PATCH_PATH_RE = re.compile(r"(?:archives\d+/)?\[[^][]+]\[[^][]+]\.ts")


def repository_path(owner: str, repo: str) -> str:
    return f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}"


def repository_file(token: str, owner: str, repo: str, ref: str, path: str) -> tuple[str | None, str | None]:
    encoded_path = urllib.parse.quote(path, safe="")
    query = urllib.parse.urlencode({"ref": ref})
    status, data = api_request(token, "GET", f"{repository_path(owner, repo)}/contents/{encoded_path}?{query}")
    if status == 404:
        return None, None
    if status != 200 or not isinstance(data, dict) or data.get("encoding") != "base64":
        raise RuntimeError(f"cannot read {owner}/{repo}:{ref}/{path}: HTTP {status}")
    try:
        content = base64.b64decode(data.get("content") or "").decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"invalid content for {owner}/{repo}:{ref}/{path}") from exc
    return content, str(data.get("sha") or "") or None


def drop_local_patch(token: str, conflict: dict, article: dict) -> str:
    repo = str(conflict.get("repository") or "")
    path = str(article.get("path") or "")
    if not repo or not PATCH_PATH_RE.fullmatch(path):
        raise RuntimeError("OCR conflict contains an invalid patch path")
    current_content, current_sha = repository_file(token, "anftm", repo, "ocr_patch", path)
    if current_content is None or not current_sha:
        return "already absent"
    upstream_ref = str(conflict.get("upstream_ocr_patch") or "")
    upstream_content, _upstream_sha = repository_file(
        token, "banned-historical-archives", repo, upstream_ref, path,
    )
    endpoint = f"{repository_path('anftm', repo)}/contents/{urllib.parse.quote(path, safe='')}"
    if upstream_content is None:
        response_or_fail(token, "DELETE", endpoint, (200,), {
            "branch": "ocr_patch",
            "message": f"Drop OCR patch for {article.get('article_id') or path}",
            "sha": current_sha,
        })
        return "deleted local-only patch"
    if upstream_content == current_content:
        return "already matches upstream"
    response_or_fail(token, "PUT", endpoint, (200, 201), {
        "branch": "ocr_patch",
        "message": f"Reset OCR patch for {article.get('article_id') or path} to upstream",
        "content": base64.b64encode(upstream_content.encode()).decode(),
        "sha": current_sha,
    })
    return "reset to upstream"


def selected_indices(raw: str, count: int) -> list[int]:
    if raw.lower() == "all":
        return list(range(1, count + 1))
    values = sorted({int(value) for value in re.split(r"[\s,]+", raw) if value})
    if not values or values[0] < 1 or values[-1] > count:
        raise RuntimeError(f"article numbers must be between 1 and {count}")
    return values


def update_issue_body(body: str, conflict: dict, decisions: dict) -> str:
    marker = f"<!-- proofreading-ocr-decisions:{encode_marker(decisions)} -->"
    if not DECISIONS_RE.search(body) or not REVIEW_BLOCK_RE.search(body):
        raise RuntimeError("OCR conflict issue is missing review state markers")
    body = DECISIONS_RE.sub(marker, body, count=1)
    status = "\n".join(review_status(conflict, decisions)).strip()
    return REVIEW_BLOCK_RE.sub(status, body, count=1)


def dispatch_rebuild(token: str, archive_id: int, issue_number: int) -> None:
    response_or_fail(
        token, "POST", f"{full_repo_path(TRACKER_REPOSITORY)}/dispatches", (204,), {
            "event_type": "ocr-rebase-reviewed",
            "client_payload": {
                "archive_id": str(archive_id),
                "allow_ocr_patch_rebase": "true",
                "issue_number": issue_number,
            },
        },
    )


def main() -> None:
    archive_token = os.environ.get("GH_PAT", "")
    tracker_token = os.environ.get("TRACKER_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
    if not archive_token or not tracker_token:
        raise RuntimeError("GH_PAT and TRACKER_TOKEN are required")
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    event = json.loads(Path(event_path).read_text(encoding="utf-8")) if event_path else {}
    issue = event.get("issue") or {}
    comment = event.get("comment") or {}
    command = COMMAND_RE.fullmatch(str(comment.get("body") or ""))
    if not command:
        return
    actor = str(comment.get("user", {}).get("login") or "")
    if not authorized(tracker_token, actor):
        raise RuntimeError(f"{actor or 'commenter'} is not authorized to review OCR conflicts")
    body = str(issue.get("body") or "")
    conflict = decode_marker(CONFLICT_RE, body)
    decisions = decode_marker(DECISIONS_RE, body)
    articles = conflict.get("articles") if isinstance(conflict.get("articles"), list) else []
    archive_id = int(conflict.get("archive_id"))
    issue_number = int(issue.get("number") or 0)
    if not articles or not issue_number:
        raise RuntimeError("OCR conflict issue payload is invalid")
    action = command.group(1).lower()
    indices = selected_indices(command.group(2), len(articles))
    results = []
    for index in indices:
        article = articles[index - 1]
        path = str(article.get("path") or "")
        if decisions.get(path) == "drop" and action == "keep":
            raise RuntimeError(f"article {index} was already dropped and cannot be restored by /ocr-keep")
        detail = drop_local_patch(archive_token, conflict, article) if action == "drop" else "kept"
        decisions[path] = action
        results.append(f"{index}. {article.get('article_id') or path}: {detail}")
    updated_body = update_issue_body(body, conflict, decisions)
    response_or_fail(
        tracker_token, "PATCH", f"{full_repo_path(TRACKER_REPOSITORY)}/issues/{issue_number}",
        (200,), {"body": updated_body},
    )
    complete = all(decisions.get(str(article.get("path") or "")) in {"keep", "drop"} for article in articles)
    if complete:
        dispatch_rebuild(tracker_token, archive_id, issue_number)
    summary = [f"<!-- ocr-review-command:{actor} -->", f"已执行 `/ocr-{action}`：", ""]
    summary.extend(f"- {result}" for result in results)
    if complete:
        summary.extend(["", f"全部补丁已处理，正在自动重建 Archive {archive_id}。"])
    response_or_fail(
        tracker_token, "POST", f"{full_repo_path(TRACKER_REPOSITORY)}/issues/{issue_number}/comments",
        (201,), {"body": "\n".join(summary)},
    )
    print(json.dumps({
        "archive_id": archive_id, "action": action, "indices": indices,
        "complete": complete, "decisions": decisions,
    }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
