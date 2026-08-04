#!/usr/bin/env python3
"""Submit one proofreading change to an anftm archive fork as a pull request."""

import base64
import ast
import hashlib
import json
import os
import re
import sys
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request


GITHUB_API = "https://api.github.com"
OWNER = os.environ.get("PROOFREAD_OWNER", "anftm")
REPOSITORY_PREFIX = os.environ.get(
    "PROOFREAD_REPOSITORY_PREFIX", "banned-historical-archives"
)
TRACKER_REPOSITORY = os.environ.get("PROOFREAD_TRACKER_REPOSITORY") or os.environ.get("GITHUB_REPOSITORY", "anftm/pipeline")


def api_request(token, method, path, payload=None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(f"{GITHUB_API}{path}", data=body, method=method)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "anftm-pipeline-proofread/1.0")
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


def repo_path(repo):
    return f"/repos/{urllib.parse.quote(OWNER)}/{urllib.parse.quote(repo)}"


def payload_field(request, key, default=None):
    body = request.get("body") if isinstance(request.get("body"), dict) else {}
    if key in body:
        return body[key]
    if key in request:
        return request.get(key)
    return default


def set_payload_field(request, key, value):
    body = request.get("body") if isinstance(request.get("body"), dict) else None
    if body is not None:
        body[key] = value
    else:
        request[key] = value


def full_repo_path(repository):
    owner, separator, repo = repository.partition("/")
    if not separator or not owner or not repo:
        fail("tracker repository must use owner/name format")
    return f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}"


def ref_path(repo, branch):
    return f"{repo_path(repo)}/git/ref/heads/{urllib.parse.quote(branch, safe='')}"


def branch_sha(token, repo, branch):
    status, data = api_request(token, "GET", ref_path(repo, branch))
    if status == 404:
        return None
    if status != 200:
        fail(f"cannot read {repo}/{branch} ref: {data}")
    return data.get("object", {}).get("sha")


def fail(message):
    raise RuntimeError(message)


def response_or_fail(token, method, path, expected, payload=None):
    status, data = api_request(token, method, path, payload)
    if status not in expected:
        detail = data.get("message", data) if isinstance(data, dict) else data
        fail(f"GitHub {method} {path} failed ({status}): {detail}")
    return data


def get_file(token, repo, branch, path):
    encoded = urllib.parse.quote(path, safe="")
    status, data = api_request(
        token, "GET", f"{repo_path(repo)}/contents/{encoded}?ref={urllib.parse.quote(branch)}"
    )
    if status == 404:
        return "", None
    if status != 200 or data.get("encoding") != "base64" or not data.get("content"):
        fail(f"cannot read {repo}/{branch}/{path}: {data}")
    content = base64.b64decode(data["content"]).decode("utf-8")
    return content, data.get("sha")


def patch_path(archive_id, article_id, publication_id):
    for value in (article_id, publication_id):
        if not value or not all(char.isalnum() or char in "._-" for char in value):
            fail("article_id and publication_id contain invalid characters")
    return f"archives{archive_id}/[{article_id}][{publication_id}].ts"


def append_patch(existing, patch):
    source = existing or "export default [\n];"
    index = source.rfind("]")
    if index < 0:
        fail("existing OCR patch file is not an array export")
    encoded_patch = json.dumps(patch, ensure_ascii=False)
    return source[:index] + "  " + encoded_patch + ",\n" + source[index:]


def open_pull_request(token, repo, branch):
    query = urllib.parse.urlencode({"head": f"{OWNER}:{branch}", "state": "all", "per_page": 100})
    status, data = api_request(token, "GET", f"{repo_path(repo)}/pulls?{query}")
    if status == 200 and isinstance(data, list) and data:
        for pull in data:
            if pull.get("state") == "closed" and not pull.get("merged"):
                detail_status, detail = api_request(token, "GET", f"{repo_path(repo)}/pulls/{pull.get('number')}")
                if detail_status != 200 or not detail.get("merged"):
                    continue
                pull = detail
            if pull.get("state") == "open" or pull.get("merged"):
                return {
                    "number": pull.get("number"),
                    "url": pull.get("html_url"),
                    "sha": pull.get("head", {}).get("sha"),
                    "base": pull.get("base", {}).get("ref"),
                    "head": pull.get("head", {}).get("ref") or branch,
                    "merge_commit_sha": pull.get("merge_commit_sha"),
                }
    return None


def submit_file(token, repo, base, path, content, title, description, correction_id):
    branch = f"proofread/{correction_id}-{base}"
    existing_pull = open_pull_request(token, repo, branch)
    _base_content, base_file_sha = get_file(token, repo, base, path)
    if existing_pull:
        if existing_pull.get("base") != base or existing_pull.get("head") != branch:
            fail(f"existing proofreading PR has an unexpected branch target: {existing_pull}")
        branch_content, _branch_file_sha = get_file(token, repo, branch, path)
        if branch_content != content:
            fail("existing proofreading PR branch does not contain the requested file content")
        status, files = api_request(token, "GET", f"{repo_path(repo)}/pulls/{existing_pull['number']}/files?per_page=100")
        if status != 200 or not isinstance(files, list) or {item.get("filename") for item in files} != {path}:
            fail("existing proofreading PR contains unexpected files")
        return existing_pull
    branch_revision = branch_sha(token, repo, branch)
    if branch_revision is None:
        base_revision = response_or_fail(token, "GET", ref_path(repo, base), (200,))["object"]["sha"]
        response_or_fail(
            token, "POST", f"{repo_path(repo)}/git/refs", (201,),
            {"ref": f"refs/heads/{branch}", "sha": base_revision},
        )
    branch_content, branch_file_sha = get_file(token, repo, branch, path)
    if branch_content != content:
        response_or_fail(
            token,
            "PUT",
            f"{repo_path(repo)}/contents/{urllib.parse.quote(path, safe='')}",
            (200, 201),
            {
                "branch": branch,
                "message": title,
                "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                **({"sha": branch_file_sha or base_file_sha} if branch_file_sha or base_file_sha else {}),
            },
        )
    pull = response_or_fail(
        token,
        "POST",
        f"{repo_path(repo)}/pulls",
        (201,),
        {"title": title, "head": branch, "base": base, "body": description},
    )
    return {
        "number": pull.get("number"), "url": pull.get("html_url"),
        "sha": pull.get("head", {}).get("sha"), "base": base, "head": branch,
    }


def merge_pull(token, repo, pull):
    number = pull.get("number")
    if not number:
        return False
    for _attempt in range(60):
        data = response_or_fail(token, "GET", f"{repo_path(repo)}/pulls/{number}", (200,))
        if data.get("merged"):
            return True
        if data.get("mergeable") is None:
            time.sleep(5)
            continue
        state = data.get("mergeable_state")
        if state in {"unknown", "unstable", "has_hooks"}:
            time.sleep(5)
            continue
        if not data.get("mergeable") or state != "clean":
            return False
        status, result = api_request(token, "PUT", f"{repo_path(repo)}/pulls/{number}/merge", {
            "sha": data.get("head", {}).get("sha"),
            "merge_method": "squash",
            "commit_title": data.get("title"),
        })
        if status == 200 and result.get("merged"):
            pull["merge_commit_sha"] = result.get("sha")
            return True
        if status in (405, 409):
            return False
        fail(f"pull request merge failed ({status}): {result.get('message', result)}")
    return False


def ensure_tracker_label(token, name="proofreading-review", color="b54708", description="Proofreading pull requests awaiting manual review"):
    encoded_name = urllib.parse.quote(name, safe="")
    path = f"{full_repo_path(TRACKER_REPOSITORY)}/labels/{encoded_name}"
    status, _data = api_request(token, "GET", path)
    if status == 200:
        return
    if status != 404:
        fail(f"tracker label lookup failed with HTTP {status}")
    response_or_fail(token, "POST", f"{full_repo_path(TRACKER_REPOSITORY)}/labels", (201,), {
        "name": name,
        "color": color,
        "description": description,
    })


def find_tracker_issue(token, correction_id):
    marker = f"<!-- proofreading:{correction_id} -->"
    for page in range(1, 11):
        status, issues = api_request(
            token, "GET",
            f"{full_repo_path(TRACKER_REPOSITORY)}/issues?state=all&labels=proofreading-review&per_page=100&page={page}",
        )
        if status != 200 or not isinstance(issues, list):
            fail(f"tracker issue listing failed with HTTP {status}")
        for issue in issues:
            if marker in str(issue.get("body") or ""):
                return issue
        if len(issues) < 100:
            break
    return None


def change_summary(request):
    changes = []
    patch = payload_field(request, "patch") or {}
    if patch.get("parts"):
        changes.append(f"正文段落 {len(patch['parts'])} 处")
    if patch.get("comments") or patch.get("newComments"):
        changes.append("注释")
    if patch.get("description"):
        changes.append("描述")
    metadata = payload_field(request, "metadata") or {}
    article = metadata.get("article") or {}
    source = metadata.get("source") or {}
    labels = {
        "title": "标题", "authors": "作者", "dates": "日期", "tags": "标签",
        "name": "来源名称", "author": "来源作者", "type": "来源类型", "files": "来源文件",
    }
    changes.extend(labels.get(key, key) for key in article)
    changes.extend(labels.get(key, key) for key in source)
    return changes or ["校订"]


def validate_patch(patch):
    if not isinstance(patch, dict) or patch.get("version") != 2:
        fail("patch must be a PatchV2 object")
    if set(patch) - {"version", "parts", "comments", "description", "newComments"}:
        fail("patch contains unsupported fields")
    if not isinstance(patch.get("parts"), dict) or not isinstance(patch.get("comments"), dict):
        fail("patch parts and comments must be objects")
    if not isinstance(patch.get("description", ""), str):
        fail("patch description must be a string")
    if "newComments" in patch and (
        not isinstance(patch["newComments"], list)
        or any(not isinstance(value, str) for value in patch["newComments"])
    ):
        fail("patch newComments must be a string list")
    for collection, allowed in ((patch["parts"], {"insertBefore", "insertAfter", "delete", "diff", "type"}),
                                (patch["comments"], {"insertBefore", "insertAfter", "delete", "diff"})):
        for index, change in collection.items():
            if not isinstance(index, str) or not index.isdigit() or (len(index) > 1 and index[0] == "0"):
                fail("patch index is invalid")
            if not isinstance(change, dict) or not change or set(change) - allowed:
                fail("patch operation is invalid")
            if "delete" in change and not isinstance(change["delete"], bool):
                fail("patch delete must be boolean")
            if "diff" in change and not isinstance(change["diff"], str):
                fail("patch diff must be a string")
            for key in ("insertBefore", "insertAfter"):
                if key in change and not isinstance(change[key], list):
                    fail("patch insert operation must be a list")
    if not patch["parts"] and not patch["comments"] and not patch.get("description") and "newComments" not in patch:
        fail("patch contains no changes")


def auto_merge_allowed(kind, patch, metadata):
    if kind != "proofread" or not isinstance(patch, dict) or isinstance(metadata, dict):
        return False
    if patch.get("newComments") or patch.get("description"):
        return False
    if not patch.get("parts") and not patch.get("comments"):
        return False
    changes = [*patch.get("parts", {}).values(), *patch.get("comments", {}).values()]
    if not changes or len(changes) > 3:
        return False
    cost = 0
    for change in changes:
        if not isinstance(change, dict) or set(change) != {"diff"} or not isinstance(change["diff"], str):
            return False
        for token in change["diff"].split("\t"):
            if not token or token[0] not in {"=", "-", "+"}:
                return False
            if token[0] in {"=", "-"}:
                if not token[1:].isdigit():
                    return False
                if token[0] == "-":
                    cost += int(token[1:])
            else:
                try:
                    cost += len(urllib.parse.unquote_to_bytes(token[1:].replace("+", "%2B")).decode("utf-8").encode("utf-16-le")) // 2
                except (UnicodeDecodeError, ValueError):
                    return False
    return cost <= 20


METADATA_FIELD_LABELS = {
    "title": "标题", "authors": "作者", "dates": "日期", "tags": "标签",
    "name": "来源名称", "author": "来源作者", "type": "来源类型", "files": "来源文件",
}

def apply_text_delta(text, delta):
    prefix = 0
    removed = 0
    inserted = ""
    seen_insert = False
    for token in delta.split("\t") if delta else []:
        if not token:
            continue
        operation, value = token[0], token[1:]
        if operation == "=" and not seen_insert and value.isdigit():
            prefix = int(value)
        elif operation == "-" and value.isdigit():
            removed = int(value)
        elif operation == "+":
            seen_insert = True
            inserted = urllib.parse.unquote_to_bytes(value.replace("+", "%2B")).decode("utf-8")
    return text[:prefix] + inserted + text[prefix + removed:]


def article_part_text(article, index):
    parts = article.get("parts") if isinstance(article.get("parts"), list) else []
    if index < 0 or index >= len(parts) or not isinstance(parts[index], dict):
        fail("BHA preview contains an invalid part index")
    values = list(str(parts[index].get("text") or ""))
    pivots = article.get("comment_pivots") if isinstance(article.get("comment_pivots"), list) else []
    selected = [item for item in pivots if isinstance(item, dict) and item.get("part_idx") == index]
    for pivot in sorted(selected, key=lambda item: int(item.get("offset", 0)), reverse=True):
        offset = int(pivot.get("offset", 0))
        if offset < 0 or offset > len(values):
            fail("BHA preview contains an invalid comment offset")
        values.insert(offset, f"〔{pivot.get('index')}〕")
    return "".join(values)


def fetch_bha_changes(request):
    if request.get("changed") and request.get("fulltext"):
        return
    doc_id = str(request.get("doc_id") or "")
    if not doc_id:
        return
    bha_url = os.environ.get("BHA_PUBLIC_URL", "https://vomebook-bha-search.hf.space").rstrip("/")
    preview_url = f"{bha_url}/api/preview/{urllib.parse.quote(doc_id, safe='')}"
    preview_request = urllib.request.Request(preview_url, headers={"User-Agent": "anftm-pipeline-proofread/1.0"})
    try:
        with urllib.request.urlopen(preview_request, timeout=30) as response:
            preview = json.loads(response.read())
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        fail(f"cannot load BHA text for proofreading issue: {exc}")
    article = preview.get("article") if isinstance(preview.get("article"), dict) else {}
    comments = article.get("comments") if isinstance(article.get("comments"), list) else []
    patch = payload_field(request, "patch") if isinstance(payload_field(request, "patch"), dict) else {}
    original_parts = [article_part_text(article, index) for index in range(len(article.get("parts") or []))]
    edited_parts = []
    for index, original in enumerate(original_parts):
        part_change = (patch.get("parts") or {}).get(str(index), {})
        edited_parts.extend(str(part.get("text") or "") for part in part_change.get("insertBefore") or [])
        if not part_change.get("delete"):
            edited_parts.append(apply_text_delta(original, part_change["diff"]) if "diff" in part_change else original)
        edited_parts.extend(str(part.get("text") or "") for part in part_change.get("insertAfter") or [])

    edited_comments = []
    for index, original_value in enumerate(comments, start=1):
        original = str(original_value or "")
        comment_change = (patch.get("comments") or {}).get(str(index), {})
        edited_comments.extend(str(item.get("text") or "") for item in comment_change.get("insertBefore") or [])
        if not comment_change.get("delete"):
            edited_comments.append(apply_text_delta(original, comment_change["diff"]) if "diff" in comment_change else original)
        edited_comments.extend(str(item.get("text") or "") for item in comment_change.get("insertAfter") or [])
    edited_comments.extend(str(value) for value in patch.get("newComments") or [])

    def fulltext(parts_text, comments_text):
        body = "\n".join(parts_text)
        if comments_text:
            body += "\n\n" + "\n".join(f"〔{index}〕{text}" for index, text in enumerate(comments_text, start=1))
        return body

    request["fulltext"] = {
        "original": fulltext(original_parts, [str(value or "") for value in comments]),
        "edited": fulltext(edited_parts, edited_comments),
    }
    if request.get("changed"):
        return
    changes = []
    for raw_index in sorted((patch.get("parts") or {}), key=int):
        index = int(raw_index)
        change = patch["parts"][raw_index]
        original = article_part_text(article, index)
        if "diff" in change:
            changes.append({"kind": "part", "index": index + 1, "original": original, "edited": apply_text_delta(original, change["diff"])})
        if change.get("delete"):
            changes.append({"kind": "part", "index": index + 1, "delete": True, "original": original})
        for key in ("insertBefore", "insertAfter"):
            for part in change.get(key) or []:
                changes.append({"kind": "part", "index": index + 1, "insert": key == "insertAfter", "text": part.get("text"), "part_type": part.get("type")})
        if "type" in change:
            parts = article.get("parts") if isinstance(article.get("parts"), list) else []
            old_type = parts[index].get("type") if index < len(parts) and isinstance(parts[index], dict) else None
            if old_type != change["type"]:
                changes.append({"kind": "part_type", "index": index + 1, "old": old_type, "new": change["type"]})
    for raw_index in sorted((patch.get("comments") or {}), key=int):
        index = int(raw_index) - 1
        change = patch["comments"][raw_index]
        original = str(comments[index] or "") if 0 <= index < len(comments) else ""
        if "diff" in change:
            changes.append({"kind": "comment", "index": index + 1, "original": original, "edited": apply_text_delta(original, change["diff"])})
        if change.get("delete"):
            changes.append({"kind": "comment", "index": index + 1, "delete": True, "original": original})
    for offset, text in enumerate(patch.get("newComments") or [], start=1):
        changes.append({"kind": "new_comment", "index": len(comments) + offset, "text": text})
    if patch.get("description"):
        original = str(article.get("description") or "")
        changes.append({"kind": "description", "original": original, "edited": apply_text_delta(original, patch["description"])})
    metadata = payload_field(request, "metadata") if isinstance(payload_field(request, "metadata"), dict) else {}
    for field, new_value in (metadata.get("article") or {}).items():
        if field in ("title", "authors", "dates", "tags"):
            changes.append({"kind": "metadata", "field": field, "old": article.get(field), "new": new_value})
    source_old = {
        "name": preview.get("publication_name"),
        "type": preview.get("publication_type"),
        "files": [item.get("url") for item in preview.get("source_files") or [] if isinstance(item, dict)],
    }
    for field, new_value in (metadata.get("source") or {}).items():
        changes.append({"kind": "metadata", "field": field, "old": source_old.get(field), "new": new_value})
    if not changes:
        fail("BHA preview did not contain the text needed for proofreading issue")
    request["changed"] = changes


def issue_value(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value if value is not None else "")


def metadata_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip().startswith("["):
        for parser in (json.loads, ast.literal_eval):
            try:
                parsed = parser(value)
                if isinstance(parsed, list):
                    return parsed
            except (ValueError, SyntaxError, json.JSONDecodeError):
                pass
    return None


def metadata_item(field, value):
    if field == "dates" and isinstance(value, dict):
        parts = []
        for key, suffix in (("year", "年"), ("month", "月"), ("day", "日")):
            if value.get(key) is not None:
                parts.append(f"{value[key]}{suffix}")
        return "".join(parts) or issue_value(value)
    if field == "tags" and isinstance(value, dict):
        name = str(value.get("name") or "")
        tag_type = str(value.get("type") or "")
        return f"{name}（{tag_type}）" if tag_type else name
    return issue_value(value)


def metadata_list_details(label, field, old, new):
    old_values = metadata_list(old)
    new_values = metadata_list(new)
    if old_values is None or new_values is None or old_values == new_values:
        return []
    key = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True)
    old_keys = {key(value) for value in old_values}
    new_keys = {key(value) for value in new_values}
    removed = [metadata_item(field, value) for value in old_values if key(value) not in new_keys]
    added = [metadata_item(field, value) for value in new_values if key(value) not in old_keys]
    if not removed and not added:
        return []
    lines = [f"### {label}", ""]
    lines.extend(f"- 删除：~~{value}~~" for value in removed)
    lines.extend(f"- 新增：**{value}**" for value in added)
    lines.append("")
    return lines


def change_details(request):
    lines = []

    def changed_pair(label, old, new, noun="文"):
        if old == new:
            return
        lines.extend([
            f"### {label}", "",
            f"**原{noun}**", "", fenced_text(issue_value(old)), "",
            f"**新{noun}**", "", fenced_text(issue_value(new)), "",
        ])

    for change in request.get("changed") or []:
        kind = change.get("kind")
        if kind in ("part", "comment"):
            label = f"段落 {change.get('index')}" if kind == "part" else f"注释 {change.get('index')}"
            if change.get("delete"):
                lines.extend([f"### {label}（删除）", "", "**原文**", "", fenced_text(issue_value(change.get("original"))), ""])
            elif change.get("text") is not None:
                place = "后" if change.get("insert") else "前"
                lines.extend([f"### {label}{place}插入", "", "**新增文本**", "", fenced_text(issue_value(change.get("text"))), ""])
            else:
                changed_pair(label, change.get("original"), change.get("edited"))
        elif kind == "part_type":
            changed_pair(f"段落 {change.get('index')}类型", change.get("old"), change.get("new"), "值")
        elif kind == "new_comment":
            lines.extend([f"### 新增注释 {change.get('index')}", "", "**新增文本**", "", fenced_text(issue_value(change.get("text"))), ""])
        elif kind == "description":
            changed_pair("描述", change.get("original"), change.get("edited"))
        elif kind == "metadata":
            field = change.get("field")
            label = METADATA_FIELD_LABELS.get(field, field)
            if field in {"authors", "dates", "tags", "files"}:
                list_details = metadata_list_details(label, field, change.get("old"), change.get("new"))
                if list_details:
                    lines.extend(list_details)
                elif change.get("old") != change.get("new"):
                    changed_pair(label, change.get("old"), change.get("new"), "值")
            else:
                changed_pair(label, change.get("old"), change.get("new"), "值")
    return lines


def fenced_text(value):
    text = str(value if value is not None else "")
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def append_fulltext(lines, request):
    fulltext = request.get("fulltext") if isinstance(request.get("fulltext"), dict) else {}
    if "original" in fulltext and "edited" in fulltext and fulltext["original"] != fulltext["edited"]:
        lines.extend([
            "", "## 原全文", "", fenced_text(fulltext["original"]),
            "", "## 修改后全文", "", fenced_text(fulltext["edited"]),
        ])


def upsert_tracker_issue(token, correction_id, request, repo, article_id, pulls):
    ensure_tracker_label(token)
    title_text = re.sub(r"^(?:校订审核：|校订\s*)+", "", str(request.get("title") or article_id).strip())[:100]
    pull_state = [{"repo": repo, "number": pull["number"], "url": pull["url"]} for pull in pulls]
    marker = json.dumps(pull_state, ensure_ascii=False, separators=(",", ":"))
    doc_id = str(request.get("doc_id") or "")
    bha_url = os.environ.get("BHA_PUBLIC_URL", "https://vomebook-bha-search.hf.space").rstrip("/")
    preview = f"{bha_url}/?preview={urllib.parse.quote(doc_id)}" if doc_id else ""
    lines = [
        f"<!-- proofreading:{correction_id} -->",
        f"<!-- proofreading-prs:{marker} -->",
        "## 校订审核",
        "",
        f"- Archive：`{repo}`",
        f"- Article ID：`{article_id}`",
        f"- 文章：{title_text}",
        f"- 修改：{'、'.join(change_summary(request))}",
    ]
    details = change_details(request)
    if details:
        lines.extend(["", "## 修改内容", ""])
        lines.extend(details)
    append_fulltext(lines, request)
    if preview:
        lines.append(f"- BHA 预览：{preview}")
    if payload_field(request, "description"):
        lines.extend([f"- 说明：{payload_field(request, 'description')}"])
    lines.extend([
        "",
        "## 审核方式",
        "",
        "1. 审核上方“修改内容”：正文核对原文与新文；元数据核对新增、删除或原值与新值",
        "2. 有正文修改时，对照“原全文”与“修改后全文”；需要核对扫描件时再打开 BHA 预览",
        "3. 如需确认实际提交文件，打开下方 Pull Request 查看 diff；正文与元数据可能对应不同 PR",
        "4. 全部同意：在本 Issue 评论 `/approve`（合并本 Issue 关联的全部 PR）",
        "5. 拒绝：评论 `/reject 原因`（关闭本 Issue 关联的全部 PR，并记录原因）",
        "6. 所有关联 PR 合并或关闭后，本 Issue 会自动关闭",
        "",
        "## Pull Requests",
        "",
    ])
    lines.extend(f"- [ ] [{repo}#{pull['number']}]({pull['url']})" for pull in pulls)
    body = "\n".join(lines)
    existing = find_tracker_issue(token, correction_id)
    assignee = os.environ.get("PROOFREAD_TRACKER_ASSIGNEE", "").strip()
    payload = {
        "title": f"校订审核：{title_text}",
        "body": body,
        "labels": ["proofreading-review"],
        **({"assignees": [assignee]} if assignee else {}),
    }
    if existing:
        payload["state"] = "open"
        issue = response_or_fail(
            token, "PATCH", f"{full_repo_path(TRACKER_REPOSITORY)}/issues/{existing['number']}", (200,), payload,
        )
    else:
        issue = response_or_fail(
            token, "POST", f"{full_repo_path(TRACKER_REPOSITORY)}/issues", (201,), payload,
        )
    return issue.get("html_url")


def find_auto_merge_log(token):
    marker = "<!-- proofreading-auto-merge-log -->"
    status, issues = api_request(
        token, "GET",
        f"{full_repo_path(TRACKER_REPOSITORY)}/issues?state=open&labels=proofreading-auto-merged&per_page=100",
    )
    if status != 200 or not isinstance(issues, list):
        fail(f"auto-merge log lookup failed with HTTP {status}")
    return next((issue for issue in issues if marker in str(issue.get("body") or "")), None)


def ensure_auto_merge_log(token):
    ensure_tracker_label(
        token, "proofreading-auto-merged", "1a7f37",
        "Notifications for proofreading pull requests merged automatically",
    )
    issue = find_auto_merge_log(token)
    if issue:
        return issue
    assignee = os.environ.get("PROOFREAD_TRACKER_ASSIGNEE", "").strip()
    payload = {
        "title": "校订自动合并记录",
        "body": "<!-- proofreading-auto-merge-log -->\n自动合并的低风险正文校订会记录在此 Issue 的评论中。",
        "labels": ["proofreading-auto-merged"],
        **({"assignees": [assignee]} if assignee else {}),
    }
    return response_or_fail(token, "POST", f"{full_repo_path(TRACKER_REPOSITORY)}/issues", (201,), payload)


def auto_merge_comment_exists(token, issue_number, correction_id):
    marker = f"<!-- auto-merged:{correction_id} -->"
    for page in range(1, 11):
        status, comments = api_request(
            token, "GET",
            f"{full_repo_path(TRACKER_REPOSITORY)}/issues/{issue_number}/comments?per_page=100&page={page}",
        )
        if status != 200 or not isinstance(comments, list):
            fail(f"auto-merge comment listing failed with HTTP {status}")
        if any(marker in str(comment.get("body") or "") for comment in comments):
            return True
        if len(comments) < 100:
            return False
    return False


def notify_auto_merged(token, correction_id, request, repo, article_id, pulls):
    issue = ensure_auto_merge_log(token)
    if auto_merge_comment_exists(token, issue["number"], correction_id):
        return issue.get("html_url")
    doc_id = str(request.get("doc_id") or "")
    bha_url = os.environ.get("BHA_PUBLIC_URL", "https://vomebook-bha-search.hf.space").rstrip("/")
    preview = f"{bha_url}/?preview={urllib.parse.quote(doc_id)}" if doc_id else ""
    lines = [
        f"<!-- auto-merged:{correction_id} -->",
        f"**已自动合并：{'、'.join(change_summary(request))}**",
        "",
        f"- Archive：`{repo}`",
        f"- Article ID：`{article_id}`",
    ]
    details = change_details(request)
    if details:
        lines.extend(["", "## 修改内容", ""])
        lines.extend(details)
    append_fulltext(lines, request)
    if preview:
        lines.append(f"- BHA 预览：{preview}")
    for pull in pulls:
        lines.append(f"- 已合并 PR：[{repo}#{pull['number']}]({pull['url']})")
        lines.append(f"  - 来源分支：`{pull.get('head', 'proofread')}`；目标分支：`{pull.get('base', 'ocr_patch')}`；合并提交：`{pull.get('merge_commit_sha') or '待查询'}`")
    lines.extend([
        "",
        f"如需撤回，请在本 Issue 评论：`/proofread-revert {correction_id} CONFIRM`。",
        "撤回操作只会创建人工审核的 revert PR，不会直接修改仓库。",
    ])
    response_or_fail(
        token, "POST", f"{full_repo_path(TRACKER_REPOSITORY)}/issues/{issue['number']}/comments", (201,),
        {"body": "\n".join(lines)},
    )
    return issue.get("html_url")


def update_config(existing, request):
    helper = os.path.join(os.path.dirname(__file__), "update_archive_config.mjs")
    process = subprocess.run(
        ["node", helper],
        input=json.dumps({
            "content": existing,
            "article_id": request.get("article_id"),
            "locator": payload_field(request, "locator"),
            "metadata": payload_field(request, "metadata"),
        }, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=False,
        # Config is fetched from a fork and evaluated by the helper. Never expose
        # the archive-write token to that process or to code executed in its VM.
        env={"PATH": os.environ.get("PATH", "")},
    )
    if process.returncode != 0:
        fail(f"config update failed: {process.stderr.strip()}")
    try:
        result = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        fail(f"config update returned invalid JSON: {exc}")
    if not isinstance(result.get("content"), str) or not isinstance(result.get("article_id"), str):
        fail("config update returned an invalid result")
    return result["content"], result["article_id"]


def load_request():
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        fail("GITHUB_EVENT_PATH is required")
    with open(event_path, encoding="utf-8") as event_file:
        event = json.load(event_file)
    request = event.get("inputs") or event.get("client_payload") or {}
    if not isinstance(request, dict):
        fail("workflow payload must be an object")
    return request


def main():
    token = os.environ.get("GH_PAT", "")
    if not token:
        fail("GH_PAT is required")
    request = load_request()
    try:
        archive_id = int(request.get("archive_id"))
    except (TypeError, ValueError):
        fail("archive_id must be an integer")
    if archive_id < 0 or archive_id > 31:
        fail("archive_id must be between 0 and 31")

    kind = request.get("kind", "ocr_patch")
    if kind not in ("proofread", "ocr_patch", "config"):
        fail("kind must be proofread, ocr_patch, or config")
    repo = f"{REPOSITORY_PREFIX}{archive_id}"
    correction_id = hashlib.sha256(
        json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    if kind == "proofread":
        article_id = str(request.get("article_id", ""))
        publication_id = str(request.get("publication_id", ""))
        patch = payload_field(request, "patch", payload_field(request, "patch_json"))
        metadata = payload_field(request, "metadata", payload_field(request, "metadata_json"))
        if isinstance(patch, str):
            if patch.strip():
                try:
                    patch = json.loads(patch)
                except json.JSONDecodeError as exc:
                    fail(f"patch_json is invalid JSON: {exc}")
                set_payload_field(request, "patch", patch)
            else:
                patch = None
        if isinstance(metadata, str):
            if metadata.strip():
                try:
                    metadata = json.loads(metadata)
                except json.JSONDecodeError as exc:
                    fail(f"metadata_json is invalid JSON: {exc}")
                set_payload_field(request, "metadata", metadata)
            else:
                metadata = None
        if patch is None and not isinstance(metadata, dict):
            fail("proofread requires patch or metadata")
        if patch is not None:
            validate_patch(patch)
        fetch_bha_changes(request)
        correction_id = hashlib.sha256(json.dumps({
            "archive_id": archive_id, "kind": "proofread", "article_id": article_id,
            "publication_id": publication_id, "patch": patch, "metadata": metadata,
        }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:12]
        patch_path(archive_id, article_id, publication_id)
        title = request.get("title") or f"校订 {article_id}"
        description = payload_field(request, "description") or "由 BHA 校订后端提交。"
        pull_requests = []
        new_article_id = article_id
        if isinstance(metadata, dict):
            config_path = f"{publication_id}.ts"
            config_content, _sha = get_file(token, repo, "config", config_path)
            if not config_content:
                fail(f"config file does not exist: {config_path}")
            updated_config, new_article_id = update_config(config_content, request)
            pull_requests.append(submit_file(
                token, repo, "config", config_path, updated_config,
                title, description, correction_id,
            ))
        if patch is not None or new_article_id != article_id:
            target_path = patch_path(archive_id, new_article_id, publication_id)
            target_content, _sha = get_file(token, repo, "ocr_patch", target_path)
            if not target_content and new_article_id != article_id:
                old_path = patch_path(archive_id, article_id, publication_id)
                target_content, _sha = get_file(token, repo, "ocr_patch", old_path)
            if patch is not None:
                target_content = append_patch(target_content, patch)
            elif not target_content:
                target_content = "export default [\n];"
            pull_requests.append(submit_file(
                token, repo, "ocr_patch", target_path, target_content,
                title, description, correction_id,
            ))
        auto_merged = []
        if request.get("auto_merge") is True and auto_merge_allowed(kind, patch, metadata):
            auto_merged = [pull["url"] for pull in pull_requests if merge_pull(token, repo, pull)]
        auto_merged_urls = set(auto_merged)
        pending_pulls = [pull for pull in pull_requests if pull["url"] not in auto_merged_urls]
        tracker_issue = None
        auto_merge_log = None
        tracker_token = os.environ.get("TRACKER_TOKEN", "")
        if pending_pulls and tracker_token:
            tracker_issue = upsert_tracker_issue(
                tracker_token, correction_id, request, repo, new_article_id, pending_pulls,
            )
        merged_pulls = [pull for pull in pull_requests if pull["url"] in auto_merged_urls]
        if merged_pulls and tracker_token:
            auto_merge_log = notify_auto_merged(
                tracker_token, correction_id, request, repo, new_article_id, merged_pulls,
            )
        print(json.dumps({
            "repository": repo,
            "article_id": new_article_id,
            "pull_requests": [pull["url"] for pull in pull_requests],
            "auto_merged": auto_merged,
            "tracker_issue": tracker_issue,
            "auto_merge_log": auto_merge_log,
        }, ensure_ascii=False))
        return
    base = kind
    if kind == "ocr_patch":
        path = patch_path(archive_id, request.get("article_id"), request.get("publication_id"))
        patch_json = payload_field(request, "patch_json", payload_field(request, "patch"))
        if isinstance(patch_json, str):
            try:
                patch = json.loads(patch_json)
            except json.JSONDecodeError as exc:
                fail(f"patch_json is invalid JSON: {exc}")
        else:
            patch = patch_json
        if patch is None:
            fail("patch_json is required")
    else:
        publication_id = str(request.get("publication_id", ""))
        metadata = payload_field(request, "metadata", payload_field(request, "metadata_json"))
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError as exc:
                fail(f"metadata_json is invalid JSON: {exc}")
            set_payload_field(request, "metadata", metadata)
        if publication_id and isinstance(metadata, dict):
            if not all(char.isalnum() or char in "._-" for char in publication_id):
                fail("publication_id contains invalid characters")
            path = f"{publication_id}.ts"
        else:
            path = str(request.get("path", ""))
            if not re.fullmatch(r"[A-Za-z0-9_.-]+\.ts", path):
                fail("config path is invalid")
            if not isinstance(request.get("content"), str):
                fail("config content is required")

    existing, _existing_sha = get_file(token, repo, base, path)
    if kind == "ocr_patch":
        content = append_patch(existing, patch)
    elif isinstance(payload_field(request, "metadata"), dict):
        content, _new_article_id = update_config(existing, request)
    else:
        content = request["content"]
    title = request.get("title") or f"校订 {path}"
    pull = submit_file(
        token, repo, base, path, content, title,
        payload_field(request, "description") or f"自动提交到 {repo}/{base}，合并后由仓库 workflow 生成后续数据。",
        correction_id,
    )
    print(json.dumps({"repository": repo, "path": path, "pull_request": pull["url"]}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
