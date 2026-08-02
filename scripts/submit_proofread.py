#!/usr/bin/env python3
"""Submit one proofreading change to an anftm archive fork as a pull request."""

import base64
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
    patch = request.get("patch") or {}
    if patch.get("parts"):
        changes.append(f"正文段落 {len(patch['parts'])} 处")
    if patch.get("comments") or patch.get("newComments"):
        changes.append("注释")
    if patch.get("description"):
        changes.append("描述")
    metadata = request.get("metadata") or {}
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


def upsert_tracker_issue(token, correction_id, request, repo, article_id, pulls):
    ensure_tracker_label(token)
    title_text = " ".join(str(request.get("title") or article_id).split())[:100]
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
        f"- 修改：{'、'.join(change_summary(request))}",
    ]
    if preview:
        lines.append(f"- BHA 预览：{preview}")
    if request.get("description"):
        lines.extend([f"- 说明：{request['description']}"])
    lines.extend(["", "## Pull Requests", ""])
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
            "locator": request.get("locator"),
            "metadata": request["metadata"],
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
        patch = request.get("patch", request.get("patch_json"))
        metadata = request.get("metadata", request.get("metadata_json"))
        if isinstance(patch, str):
            if patch.strip():
                try:
                    patch = json.loads(patch)
                except json.JSONDecodeError as exc:
                    fail(f"patch_json is invalid JSON: {exc}")
                request["patch"] = patch
            else:
                patch = None
        if isinstance(metadata, str):
            if metadata.strip():
                try:
                    metadata = json.loads(metadata)
                except json.JSONDecodeError as exc:
                    fail(f"metadata_json is invalid JSON: {exc}")
                request["metadata"] = metadata
            else:
                metadata = None
        if patch is None and not isinstance(metadata, dict):
            fail("proofread requires patch or metadata")
        if patch is not None:
            validate_patch(patch)
        correction_id = hashlib.sha256(json.dumps({
            "archive_id": archive_id, "kind": "proofread", "article_id": article_id,
            "publication_id": publication_id, "patch": patch, "metadata": metadata,
        }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:12]
        patch_path(archive_id, article_id, publication_id)
        title = request.get("title") or f"校订 {article_id}"
        description = request.get("description") or "由 BHA 校订后端提交。"
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
        patch_json = request.get("patch_json", request.get("patch"))
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
        metadata = request.get("metadata", request.get("metadata_json"))
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError as exc:
                fail(f"metadata_json is invalid JSON: {exc}")
            request["metadata"] = metadata
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
    elif isinstance(request.get("metadata"), dict):
        content, _new_article_id = update_config(existing, request)
    else:
        content = request["content"]
    title = request.get("title") or f"校订 {path}"
    pull = submit_file(
        token, repo, base, path, content, title,
        request.get("description") or f"自动提交到 {repo}/{base}，合并后由仓库 workflow 生成后续数据。",
        correction_id,
    )
    print(json.dumps({"repository": repo, "path": path, "pull_request": pull["url"]}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
