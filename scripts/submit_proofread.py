#!/usr/bin/env python3
"""Submit one proofreading change to an anftm archive fork as a pull request."""

import base64
import hashlib
import json
import os
import sys
import subprocess
import urllib.error
import urllib.parse
import urllib.request


GITHUB_API = "https://api.github.com"
OWNER = os.environ.get("PROOFREAD_OWNER", "anftm")
REPOSITORY_PREFIX = os.environ.get(
    "PROOFREAD_REPOSITORY_PREFIX", "banned-historical-archives"
)


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
    query = urllib.parse.urlencode({"head": f"{OWNER}:{branch}", "state": "open"})
    status, data = api_request(token, "GET", f"{repo_path(repo)}/pulls?{query}")
    if status == 200 and isinstance(data, list) and data:
        return data[0].get("html_url")
    return None


def submit_file(token, repo, base, path, content, title, description, correction_id):
    branch = f"proofread/{correction_id}-{base}"
    existing_pull = open_pull_request(token, repo, branch)
    if existing_pull:
        return existing_pull
    _base_content, base_file_sha = get_file(token, repo, base, path)
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
    return pull["html_url"]


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
        print(json.dumps({
            "repository": repo,
            "article_id": new_article_id,
            "pull_requests": pull_requests,
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
            if not path or ".." in path:
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
    print(json.dumps({"repository": repo, "path": path, "pull_request": pull}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
