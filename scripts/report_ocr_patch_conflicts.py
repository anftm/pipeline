#!/usr/bin/env python3
"""Create or close tracker issues for OCR baseline/proofreading conflicts."""

import html
import difflib
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from .submit_proofread import TRACKER_REPOSITORY, api_request, ensure_tracker_label, full_repo_path, response_or_fail
except ImportError:
    from submit_proofread import TRACKER_REPOSITORY, api_request, ensure_tracker_label, full_repo_path, response_or_fail


REPORT_PATH = Path(os.environ.get("BHA_OCR_CONFLICT_REPORT", "/tmp/bha-ocr-patch-conflicts.json"))
LABEL = "proofreading-ocr-conflict"
MARKER_RE = re.compile(r"<!-- proofreading-ocr-rebase:(\d+) -->")
CONFLICT_RE = re.compile(r"<!-- proofreading-ocr-conflict:([A-Za-z0-9_-]+) -->")
DECISIONS_RE = re.compile(r"<!-- proofreading-ocr-decisions:([A-Za-z0-9_-]+) -->")
BHA_URL = os.environ.get("BHA_BASE_URL", "https://vomebook-bha-search.hf.space").rstrip("/")


def tracker_issues(token: str) -> list[dict]:
    issues = []
    for page in range(1, 101):
        status, data = api_request(
            token, "GET",
            f"{full_repo_path(TRACKER_REPOSITORY)}/issues?state=open&labels={LABEL}&per_page=100&page={page}",
        )
        if status != 200 or not isinstance(data, list):
            raise RuntimeError(f"OCR conflict issue listing failed with HTTP {status}")
        issues.extend(data)
        if len(data) < 100:
            return issues
    raise RuntimeError("OCR conflict issue listing exceeded pagination limit")


def preview(doc_id: str | None) -> dict:
    if not doc_id:
        return {}
    url = f"{BHA_URL}/api/preview/{urllib.parse.quote(doc_id, safe='')}"
    request = urllib.request.Request(url, headers={"User-Agent": "anftm-pipeline-ocr-conflict/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            value = json.load(response)
            return value if isinstance(value, dict) else {}
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError):
        return {}


def text_diff(original: str, edited: str, original_name: str, edited_name: str, limit: int = 5000) -> str:
    if original == edited:
        return "无文本差异。"
    value = "\n".join(difflib.unified_diff(
        original.splitlines(), edited.splitlines(),
        fromfile=original_name, tofile=edited_name, lineterm="", n=2,
    ))
    if len(value) > limit:
        value = value[:limit] + "\n...差异过长，已截断；请打开文章预览继续核对。"
    return f"<pre>{html.escape(value)}</pre>"


def encode_marker(value: dict) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_marker(pattern: re.Pattern, body: str, default: dict | None = None) -> dict:
    match = pattern.search(body)
    if not match:
        return dict(default or {})
    try:
        raw = base64.urlsafe_b64decode(match.group(1) + "=" * (-len(match.group(1)) % 4))
        value = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return dict(default or {})
    return value if isinstance(value, dict) else dict(default or {})


def compact_conflict(conflict: dict) -> dict:
    return {
        key: conflict.get(key)
        for key in (
            "archive_id", "repository", "old_ocr_cache", "new_ocr_cache",
            "mirror_ocr_patch", "upstream_ocr_patch",
        )
    } | {"articles": [
        {key: article.get(key) for key in ("path", "article_id", "publication_id", "doc_id")}
        for article in conflict.get("articles") or []
    ]}


def review_status(conflict: dict, decisions: dict) -> list[str]:
    lines = ["<!-- ocr-review-start -->", "## 审核状态", ""]
    labels = {"keep": "保留补丁", "drop": "取消补丁"}
    for index, article in enumerate(conflict.get("articles") or [], start=1):
        path = str(article.get("path") or "")
        decision = str(decisions.get(path) or "")
        checked = "x" if decision in labels else " "
        suffix = f"：**{labels[decision]}**" if decision in labels else "：待处理"
        lines.append(f"- [{checked}] {index}. `{article.get('article_id') or path}`{suffix}")
    lines.extend([
        "",
        "评论 `/ocr-keep 1 3` 保留补丁；评论 `/ocr-drop 2` 取消补丁。可一次处理多个编号。",
        "<!-- ocr-review-end -->",
        "",
    ])
    return lines


def render_issue(conflict: dict, decisions: dict | None = None) -> str:
    decisions = decisions or {}
    archive_id = int(conflict["archive_id"])
    repository = str(conflict["repository"])
    mirror_patch = str(conflict["mirror_ocr_patch"])
    upstream_patch = str(conflict["upstream_ocr_patch"])
    marker = f"<!-- proofreading-ocr-rebase:{archive_id} -->"
    lines = [
        marker,
        f"<!-- proofreading-ocr-conflict:{encode_marker(compact_conflict(conflict))} -->",
        f"<!-- proofreading-ocr-decisions:{encode_marker(decisions)} -->",
        f"Archive {archive_id} 的 OCR 基线已经变化，但 fork 中仍有未被上游吸收的人工校对补丁。",
        "自动重建已停止，以免旧补丁静默应用到错误的段落或字符位置。",
        "",
        "## 基线",
        "",
        f"- 旧 `ocr_cache`：`{conflict['old_ocr_cache']}`",
        f"- 新 `ocr_cache`：`{conflict['new_ocr_cache']}`",
        f"- 本地 `ocr_patch`：`{mirror_patch}`",
        f"- 上游 `ocr_patch`：`{upstream_patch}`",
        f"- 潜在冲突文章：{len(conflict.get('articles') or [])} 篇",
        "",
    ]
    lines.extend(review_status(conflict, decisions))
    lines.extend(["## 文章", ""])
    articles = conflict.get("articles") or []
    diff_limit = max(200, min(5000, 40000 // max(1, len(articles)) // 2))
    excerpt_limit = max(100, min(500, 8000 // max(1, len(articles))))
    for index, article in enumerate(articles, start=1):
        doc_id = article.get("doc_id")
        current = preview(doc_id)
        title = str(current.get("title") or article.get("article_id") or article.get("path") or f"文章 {index}")
        preview_url = f"{BHA_URL}/?preview={urllib.parse.quote(str(doc_id))}" if doc_id else ""
        patch_url = (
            f"https://github.com/anftm/{urllib.parse.quote(repository)}/blob/{mirror_patch}/"
            f"{urllib.parse.quote(str(article.get('path') or ''), safe='/[]')}"
        )
        content = str(current.get("content") or "").strip()
        excerpt = content[:excerpt_limit] + ("..." if len(content) > excerpt_limit else "")
        new_ocr = article.get("new_ocr") if isinstance(article.get("new_ocr"), dict) else {}
        candidate = article.get("patched_candidate") if isinstance(article.get("patched_candidate"), dict) else {}
        new_content = str(new_ocr.get("content") or "")
        candidate_content = str(candidate.get("content") or "")
        lines.extend([
            f"<details><summary>{index}. {html.escape(title)}</summary>",
            "",
            f"- Article ID：`{article.get('article_id') or '无法解析'}`",
            f"- Publication ID：`{article.get('publication_id') or '无法解析'}`",
            f"- [当前校订版预览]({preview_url})" if preview_url else "- 当前校订版预览：无法生成",
            f"- [本地补丁文件]({patch_url})",
            "",
        ])
        if excerpt:
            lines.extend(["<pre>", html.escape(excerpt), "</pre>", ""])
        else:
            lines.extend(["当前生产预览不可用；请按 Article ID 和补丁文件核对。", ""])
        if new_ocr:
            lines.extend([
                "### 当前校订版 -> 新版 OCR",
                "",
                text_diff(content, new_content, "当前校订版", "新版 OCR", diff_limit),
                "",
            ])
        else:
            lines.extend([
                "### 新版 OCR",
                "",
                f"无法生成：{html.escape(str(article.get('new_ocr_error') or conflict.get('new_ocr_error') or '未知错误'))}",
                "",
            ])
        if candidate:
            lines.extend([
                "### 新版 OCR -> 套用旧补丁后的候选版",
                "",
                text_diff(new_content, candidate_content, "新版 OCR", "候选版", diff_limit),
                "",
            ])
        else:
            lines.extend([
                "### 套用旧补丁后的候选版",
                "",
                f"无法生成：{html.escape(str(article.get('patched_candidate_error') or conflict.get('patched_candidate_error') or '未知错误'))}",
                "",
            ])
        lines.extend(["</details>", ""])
    lines.extend([
        "## 处理方式",
        "",
        "逐篇检查两段差异，并对照扫描原件：",
        "",
        "1. “当前校订版 -> 新版 OCR”显示上游更新改变了什么。",
        "2. “新版 OCR -> 候选版”显示旧补丁现在实际会改动什么。",
        "3. 只有候选版仍符合扫描原件时才能确认重基线。",
        "",
        "使用上方评论命令处理完全部文章后，机器人会自动触发 Archive "
        f"{archive_id} 的重建；成功建立新基线后，本 Issue 自动关闭。",
    ])
    body = "\n".join(lines)
    if len(body.encode("utf-8")) > 60000:
        raise RuntimeError(f"archive {archive_id} OCR conflict issue exceeds GitHub body limit")
    return body


def upsert_conflicts(token: str, conflicts: list[dict], issues: list[dict]) -> None:
    ensure_tracker_label(
        token, LABEL, "d73a4a", "OCR baseline updates blocked by local proofreading patches",
    )
    by_archive = {}
    for issue in issues:
        match = MARKER_RE.search(str(issue.get("body") or ""))
        if match:
            by_archive[int(match.group(1))] = issue
    for conflict in conflicts:
        archive_id = int(conflict["archive_id"])
        payload = {
            "title": f"OCR 更新冲突：Archive {archive_id}",
            "body": render_issue(conflict),
            "labels": [LABEL],
        }
        existing = by_archive.get(archive_id)
        decisions = decode_marker(DECISIONS_RE, str(existing.get("body") or "")) if existing else {}
        active_paths = {str(article.get("path") or "") for article in conflict.get("articles") or []}
        decisions = {path: value for path, value in decisions.items() if path in active_paths}
        payload["body"] = render_issue(conflict, decisions)
        if existing:
            response_or_fail(
                token, "PATCH", f"{full_repo_path(TRACKER_REPOSITORY)}/issues/{existing['number']}",
                (200,), payload,
            )
        else:
            response_or_fail(token, "POST", f"{full_repo_path(TRACKER_REPOSITORY)}/issues", (201,), payload)


def selected_archives() -> set[int] | None:
    value = os.environ.get("ARCHIVE_ID", "all")
    if value == "all":
        return None
    return {int(item.strip()) for item in value.split(",") if item.strip()}


def close_resolved(token: str, issues: list[dict]) -> None:
    selected = selected_archives()
    for issue in issues:
        match = MARKER_RE.search(str(issue.get("body") or ""))
        if not match:
            continue
        archive_id = int(match.group(1))
        if selected is not None and archive_id not in selected:
            continue
        response_or_fail(
            token, "PATCH", f"{full_repo_path(TRACKER_REPOSITORY)}/issues/{issue['number']}",
            (200,), {"state": "closed", "state_reason": "completed"},
        )


def main() -> None:
    token = os.environ.get("TRACKER_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("TRACKER_TOKEN or GITHUB_TOKEN is required")
    issues = tracker_issues(token)
    if REPORT_PATH.exists():
        report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
        conflicts = report.get("conflicts") if isinstance(report, dict) else None
        if not isinstance(conflicts, list):
            raise RuntimeError("invalid OCR conflict report")
        upsert_conflicts(token, conflicts, issues)
    elif os.environ.get("BUILD_OUTCOME") == "success":
        close_resolved(token, issues)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
