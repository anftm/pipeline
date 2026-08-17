#!/usr/bin/env python3
"""Build parsed branches for anftm archives whose source branches changed."""

import base64
import hashlib
import html
import json
import os
import re
import subprocess
import shutil
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from .github_read import json_request
except ImportError:
    from github_read import json_request


GITHUB_API = "https://api.github.com"
MIRROR_OWNER = os.environ.get("MIRROR_OWNER", "anftm")
UPSTREAM_OWNER = os.environ.get("UPSTREAM_OWNER", "banned-historical-archives")
REPOSITORY_PREFIX = os.environ.get("BHA_REPOSITORY_PREFIX", "banned-historical-archives")
STATE_PATH = Path(os.environ.get("BHA_PARSED_INPUT_STATE", "state/bha-parsed-inputs.json"))
CANDIDATE_PATH = Path(os.environ.get("BHA_PARSED_INPUT_CANDIDATE", "/tmp/bha-parsed-inputs.json"))
OCR_CONFLICT_PATH = Path(os.environ.get("BHA_OCR_CONFLICT_REPORT", "/tmp/bha-ocr-patch-conflicts.json"))
ARCHIVE_ID = os.environ.get("ARCHIVE_ID", "all")
FORCE_REBUILD = os.environ.get("FORCE_REBUILD", "false").lower() == "true"
ALLOW_OCR_PATCH_REBASE = os.environ.get("ALLOW_OCR_PATCH_REBASE", "false").lower() == "true"
INPUT_BRANCHES = ("main", "config", "ocr_cache", "ocr_patch")
PARSED_BRANCH = "parsed"
PATCH_LAYOUT_VERSION = 2
LEGACY_IMAGE_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
LEGACY_FONT_TAG_RE = re.compile(r"</?font\b[^>]*>", re.IGNORECASE)
LEGACY_HTML_TAG_RE = re.compile(
    r"</?(?:html|head|body|title|img|font|br|span|b|strong|em|i|u|p|pre|div|a|sup|sub|hr|blockquote|h[1-6]|ul|ol|li|table|thead|tbody|tr|td|th)\b[^>]*>",
    re.IGNORECASE,
)
ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")
CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
WRAPPED_AUTHOR_RE = re.compile(r"^\s*(?:\((.*)\)|（(.*)）)\s*$", re.DOTALL)
AUTHOR_IMAGE_RE = re.compile(r"^\s*(?:<\s*[,，]?\s*)?img\s*=\s*[0-9a-z]+>?\s*$", re.IGNORECASE)
AUTHOR_IMAGE_FRAGMENT_RE = re.compile(r"<?\s*img\s*=\s*[0-9a-z]+>?", re.IGNORECASE)
NON_AUTHOR_BRACKET_SEGMENT_RE = re.compile(
    r"\s*[；;、,，]?\s*[\[［]\s*(?:机密|绝密|收时\s*\d+|\d+)\s*[\]］]",
    re.IGNORECASE,
)
SQUARE_BRACKET_RE = re.compile(r"[\[\]［］]")
TRUNCATED_LATIN_ANNOTATION_RE = re.compile(r"\s*[（(][A-Za-z][^）)]*$")
OCR_MARKUP_RE = re.compile(r"〖-?[A-Za-z]{2}[/；;][^〗]{1,80}〗")
OCR_MARKUP_UNCLOSED_RE = re.compile(r"〖-?[A-Za-z]{2}[/；;][^〗\r\n]{1,80}$")
AUTHOR_LAYOUT_MARK_RE = re.compile(r"〖HH/换行〗\s*DW：.*$", re.IGNORECASE)
AUTHOR_LAYOUT_RESIDUE_RE = re.compile(r"^\s*DW\s*[:：]?\s*$", re.IGNORECASE)
RAW_RECORD_AUTHOR_RE = re.compile(r"〖-(?:ZQ|RQ|BH|TH|BT|FT|YT)/", re.IGNORECASE)
AUTHOR_ANGLE_NOTE_RE = re.compile(r"<([^<>]{1,80})>")
AUTHOR_ANGLE_RESIDUE_RE = re.compile(r"^\s*(?:/ct|ct)?\s*[<>]+\s*$", re.IGNORECASE)
NON_AUTHOR_ANGLE_NOTE_RE = re.compile(r"^\s*传达记录要点\s*$")
AUTHOR_PLACEHOLDER_RE = re.compile(r"[�□]|^\?|(?:锟斤拷|烫烫烫|Ã.|Â.)")
AUTHOR_TITLE_PREFIX_RE = re.compile(r"^[—─━-]{2,}")
AUTHOR_DATE_STATEMENT_RE = re.compile(
    r"^(?:19\d{2}年.+(?:批准|公布|通过|起草)|[—─]{2,}一九\S+年)"
)
AUTHOR_PURE_NOISE_RE = re.compile(r"^(?:\d|[《？])$")
AUTHOR_OCR_RESIDUE_RE = re.compile(r"^\s*/?ct\s*$", re.IGNORECASE)
AUTHOR_MISSING_BOOK_OPEN_RE = re.compile(r"^(人民日报|解放军报)》(.*)$")
AUTHOR_PUBLISHER_CREDIT_RE = re.compile(r"^军事译文出版社出版）（([^（）]+)）$")
AUTHOR_REVIEW_NOTE_RE = re.compile(r"；阅办文件；?$")
AUTHOR_MISPARSED_PROSE_RE = re.compile(r"(?:^事由：|简介：.*简介：|图为.+[。！？])")
AUTHOR_DELIMITER_PAIRS = (("《", "》"), ("【", "】"), ("『", "』"), ("「", "」"), ("“", "”"))
ARCHIVE9_JOINED_CREDIT = "王性尧/胡子婴/胡厥文/郭棣活/盛丕华/汤蒂因/荣毅仁/刘靖基/魏如代表的联合发言"
CLEANUP_ARCHIVE_IDS = {3, 9, 10, 12, 14, 20, 24, 31}
DROP_EMPTY_CONTENT_ARCHIVE_IDS = {10, 20, 24}


def api_request(token: str, method: str, path: str, payload: dict | None = None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(f"{GITHUB_API}{path}", data=body, method=method)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "anftm-pipeline-build-parsed/1.0")
    request.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    return json_request(request)


def repo_path(owner: str, repo: str) -> str:
    return f"/repos/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}"


def branch_revisions(token: str, owner: str, repo: str) -> dict[str, str]:
    required = (*INPUT_BRANCHES, PARSED_BRANCH)
    revisions = {}
    page = 1
    while True:
        status, data = api_request(
            token, "GET", f"{repo_path(owner, repo)}/branches?per_page=100&page={page}",
        )
        if status != 200 or not isinstance(data, list):
            raise RuntimeError(f"cannot list {owner}/{repo} branches: HTTP {status}")
        revisions.update({
            str(item.get("name")): str(item.get("commit", {}).get("sha") or "")
            for item in data if isinstance(item, dict)
        })
        if all(revisions.get(branch) for branch in required) or len(data) < 100:
            break
        page += 1
    missing = [branch for branch in required if not revisions.get(branch)]
    if missing:
        raise RuntimeError(f"{owner}/{repo} is missing branches: {', '.join(missing)}")
    return {branch: revisions[branch] for branch in required}


def ref_revision(token: str, owner: str, repo: str, branch: str) -> str:
    path = f"{repo_path(owner, repo)}/git/ref/heads/{urllib.parse.quote(branch, safe='')}"
    status, data = api_request(token, "GET", path)
    revision = str(data.get("object", {}).get("sha") or "") if isinstance(data, dict) else ""
    if status != 200 or not revision:
        raise RuntimeError(f"cannot read {owner}/{repo}:{branch}: HTTP {status}")
    return revision


def commit_tree_revision(token: str, owner: str, repo: str, revision: str) -> str:
    status, data = api_request(
        token, "GET", f"{repo_path(owner, repo)}/git/commits/{urllib.parse.quote(revision, safe='')}",
    )
    tree = str(data.get("tree", {}).get("sha") or "") if isinstance(data, dict) else ""
    if status != 200 or not tree:
        raise RuntimeError(f"cannot read {owner}/{repo} commit tree {revision}: HTTP {status}")
    return tree


def ocr_patch_files(token: str, owner: str, repo: str, revision: str) -> dict[str, str]:
    tree = commit_tree_revision(token, owner, repo, revision)
    status, data = api_request(
        token, "GET", f"{repo_path(owner, repo)}/git/trees/{tree}?recursive=1",
    )
    if status != 200 or not isinstance(data, dict):
        raise RuntimeError(f"cannot read {owner}/{repo} OCR patch tree: HTTP {status}")
    if data.get("truncated"):
        raise RuntimeError(f"{owner}/{repo} OCR patch tree is truncated")
    items = data.get("tree")
    if not isinstance(items, list):
        raise RuntimeError(f"{owner}/{repo} OCR patch tree is invalid")
    return {
        str(item["path"]): str(item["sha"])
        for item in items
        if isinstance(item, dict) and item.get("type") == "blob"
        and str(item.get("path") or "").endswith(".ts") and item.get("sha")
    }


def selected_archive_ids() -> list[int]:
    if ARCHIVE_ID == "all":
        return list(range(32))
    try:
        archive_ids = sorted({int(value.strip()) for value in ARCHIVE_ID.split(",") if value.strip()})
    except ValueError as exc:
        raise RuntimeError("ARCHIVE_ID must be all or comma-separated integers from 0 through 31") from exc
    if not archive_ids or archive_ids[0] < 0 or archive_ids[-1] > 31:
        raise RuntimeError("ARCHIVE_ID must be all or comma-separated integers from 0 through 31")
    return archive_ids


def load_state() -> dict:
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "archives": {}}
    if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("archives"), dict):
        raise RuntimeError("invalid BHA parsed input state")
    return state


def source_snapshot(revisions: dict[str, str], helper_revision: str) -> dict[str, str]:
    return {
        **{branch: revisions[branch] for branch in INPUT_BRANCHES},
        "helper": helper_revision,
        "patch_layout": str(PATCH_LAYOUT_VERSION),
    }


def needs_local_build(mirror: dict[str, str], upstream: dict[str, str]) -> bool:
    return any(mirror[branch] != upstream[branch] for branch in INPUT_BRANCHES)


def ocr_patch_rebase_conflict(
    token: str,
    archive_id: int,
    previous: dict | None,
    mirror: dict[str, str],
    upstream: dict[str, str],
) -> dict | None:
    if not previous or previous.get("ocr_cache") == mirror["ocr_cache"]:
        return None
    repo = f"{REPOSITORY_PREFIX}{archive_id}"
    mirror_files = ocr_patch_files(token, MIRROR_OWNER, repo, mirror["ocr_patch"])
    upstream_files = ocr_patch_files(token, UPSTREAM_OWNER, repo, upstream["ocr_patch"])
    changed_paths = sorted(
        path for path, blob in mirror_files.items()
        if upstream_files.get(path) != blob
    )
    if not changed_paths:
        return None
    if ALLOW_OCR_PATCH_REBASE:
        print(
            f"archive {archive_id}: accepting reviewed OCR baseline change "
            f"{previous.get('ocr_cache')} -> {mirror['ocr_cache']}",
        )
        return None
    articles = []
    for path in changed_paths:
        match = re.fullmatch(r"(?:archives\d+/)?\[([^][]+)]\[([^][]+)]\.ts", path)
        article_id, publication_id = match.groups() if match else (None, None)
        articles.append({
            "path": path,
            "article_id": article_id,
            "publication_id": publication_id,
            "doc_id": (
                f"{archive_id}:{len(article_id)}:{article_id}:{publication_id}"
                if article_id is not None and publication_id is not None else None
            ),
        })
    return {
        "archive_id": archive_id,
        "repository": repo,
        "old_ocr_cache": previous.get("ocr_cache"),
        "new_ocr_cache": mirror["ocr_cache"],
        "mirror_ocr_patch": mirror["ocr_patch"],
        "upstream_ocr_patch": upstream["ocr_patch"],
        "articles": articles,
    }


def write_ocr_conflicts(conflicts: list[dict]) -> None:
    OCR_CONFLICT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OCR_CONFLICT_PATH.write_text(
        json.dumps({"version": 1, "conflicts": conflicts}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def article_text(article: dict) -> str:
    values = []
    if article.get("description"):
        values.append(str(article["description"]))
    for part in article.get("parts") or []:
        if isinstance(part, dict) and part.get("text"):
            values.append(str(part["text"]))
    values.extend(str(comment) for comment in article.get("comments") or [] if comment)
    return "\n".join(values)


def parsed_article(parsed: Path, article_id: str, publication_id: str) -> dict | None:
    matches = []
    for metadata_path in parsed.glob("*/*/*.metadata"):
        if metadata_path.stem == publication_id:
            matches.extend(metadata_path.parent.glob(f"*/{article_id}.json"))
    if len(matches) > 1:
        raise RuntimeError(f"multiple parsed articles match {article_id}/{publication_id}")
    if not matches:
        return None
    article = json.loads(matches[0].read_text(encoding="utf-8"))
    if not isinstance(article, dict):
        raise RuntimeError(f"parsed article is not an object: {matches[0]}")
    return {"title": str(article.get("title") or article_id), "content": article_text(article)}


def build_ocr_conflict_previews(
    token: str,
    helper: Path,
    root: Path,
    conflict: dict,
    mirror: dict[str, str],
    upstream: dict[str, str],
) -> None:
    archive_id = int(conflict["archive_id"])
    repo = str(conflict["repository"])
    mirror_url = f"https://github.com/{MIRROR_OWNER}/{repo}.git"
    upstream_url = f"https://github.com/{UPSTREAM_OWNER}/{repo}.git"
    env = git_environment(token)
    inputs = {}
    for branch in ("main", "config", "ocr_cache"):
        target = root / f"{repo}-conflict-{branch}"
        clone_branch(mirror_url, branch, target, env, mirror[branch])
        inputs[branch] = target
    local_patch = root / f"{repo}-conflict-local-patch"
    upstream_patch = root / f"{repo}-conflict-upstream-patch"
    clone_branch(mirror_url, "ocr_patch", local_patch, env, mirror["ocr_patch"])
    clone_branch(upstream_url, "ocr_patch", upstream_patch, env, upstream["ocr_patch"])
    variants = (
        ("new_ocr", upstream_patch),
        ("patched_candidate", local_patch),
    )
    outputs = {}
    for key, patch_source in variants:
        patch_input = prepare_patch_input(
            patch_source, root / f"{repo}-conflict-{key}-patch-input", archive_id,
        )
        output_root = root / f"{repo}-conflict-{key}-parsed"
        output_root.mkdir()
        try:
            run_in_container(root, helper, [
                "npm", "run", "build_parsed", "--",
                str(inputs["config"]), str(inputs["ocr_cache"]), str(patch_input),
                str(output_root), str(inputs["main"]),
            ])
            clean_selected_archive_parsed(output_root, archive_id)
            outputs[key] = output_root
        except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
            conflict[f"{key}_error"] = str(exc)
    for article in conflict.get("articles") or []:
        article_id = article.get("article_id")
        publication_id = article.get("publication_id")
        if not article_id or not publication_id:
            continue
        for key, output_root in outputs.items():
            value = parsed_article(output_root, article_id, publication_id)
            if value is not None:
                article[key] = value
            else:
                article[f"{key}_error"] = "article was not generated"


def git_environment(token: str) -> dict[str, str]:
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = os.environ.copy()
    env.update({
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {encoded}",
        "GIT_TERMINAL_PROMPT": "0",
    })
    return env


def clean_environment() -> dict[str, str]:
    env = os.environ.copy()
    for key in ("GH_PAT", "GITHUB_TOKEN", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
        env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def push_environment(token: str, home: Path) -> dict[str, str]:
    env = git_environment(token)
    env["HOME"] = str(home)
    return env


def run(command: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=str(cwd) if cwd else None, env=env, check=True)


def run_output(command: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        command, cwd=str(cwd) if cwd else None, env=env,
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def clone_branch(repo_url: str, branch: str, target: Path, env: dict[str, str], expected_sha: str) -> None:
    run(["git", "clone", "--depth", "1", "--single-branch", "--branch", branch, repo_url, str(target)], env=env)
    if run_output(["git", "rev-parse", "HEAD"], cwd=target, env=env) != expected_sha:
        raise RuntimeError(f"{repo_url}:{branch} changed while preparing parsed data; retry the workflow")


def run_in_container(root: Path, cwd: Path, command: list[str]) -> None:
    run([
        "docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "--env", "HOME=/tmp",
        "--volume", f"{root}:{root}", "--workdir", str(cwd),
        "node:24", *command,
    ], env=clean_environment())


def prepare_helper(root: Path, env: dict[str, str], revision: str) -> Path:
    helper = root / "ocr_helper"
    clone_branch("https://github.com/banned-historical-archives/ocr_helper.git", "main", helper, env, revision)
    run_in_container(root, helper, ["npm", "install"])
    return helper


def sanitize_repository(repository: Path) -> None:
    git_dir = repository / ".git"
    if not git_dir.is_dir() or git_dir.is_symlink():
        raise RuntimeError("parsed repository metadata was replaced during generation")
    config = git_dir / "config"
    config.unlink(missing_ok=True)
    config.write_text("[core]\n\trepositoryformatversion = 0\n\tbare = false\n", encoding="utf-8")
    hooks = git_dir / "hooks"
    if hooks.exists():
        shutil.rmtree(hooks)
    hooks.mkdir()


def clean_legacy_image_markup(value):
    if isinstance(value, str):
        cleaned = value
        for _ in range(2):
            cleaned = html.unescape(cleaned)
        cleaned = LEGACY_HTML_TAG_RE.sub("", cleaned)
        cleaned = ZERO_WIDTH_RE.sub("", cleaned)
        cleaned = CONTROL_CHARACTER_RE.sub("", cleaned)
        cleaned = OCR_MARKUP_RE.sub("", cleaned)
        return OCR_MARKUP_UNCLOSED_RE.sub("", cleaned)
    if isinstance(value, list):
        return [clean_legacy_image_markup(item) for item in value]
    if isinstance(value, dict):
        cleaned = {key: clean_legacy_image_markup(item) for key, item in value.items()}
        authors = value.get("authors")
        if isinstance(authors, list):
            normalized = []
            for author in authors:
                if (not isinstance(author, str) or AUTHOR_IMAGE_RE.fullmatch(author)
                        or AUTHOR_LAYOUT_RESIDUE_RE.fullmatch(author) or RAW_RECORD_AUTHOR_RE.search(author)
                        or AUTHOR_ANGLE_RESIDUE_RE.fullmatch(author)):
                    continue
                author = AUTHOR_LAYOUT_MARK_RE.sub("", author).strip()
                author = AUTHOR_IMAGE_FRAGMENT_RE.sub("", author).strip()
                author = clean_legacy_image_markup(author)
                author = AUTHOR_ANGLE_NOTE_RE.sub(r"\1", author).strip(" <>\t\r\n")
                if NON_AUTHOR_ANGLE_NOTE_RE.fullmatch(author):
                    continue
                author = NON_AUTHOR_BRACKET_SEGMENT_RE.sub("", author)
                author = SQUARE_BRACKET_RE.sub("", author).strip()
                author = TRUNCATED_LATIN_ANNOTATION_RE.sub("", author).strip()
                if author.count("(") > author.count(")") and author.rfind("(") > 0:
                    author = author[:author.rfind("(")].strip()
                if author.count("（") > author.count("）") and author.rfind("（") > 0:
                    author = author[:author.rfind("（")].strip()
                if author.startswith("(") and ")" not in author:
                    author = author[1:].strip()
                elif author.endswith(")") and "(" not in author:
                    author = author[:-1].strip()
                if author.startswith("（") and "）" not in author:
                    author = author[1:].strip()
                elif author.endswith("）") and "（" not in author:
                    author = author[:-1].strip()
                match = WRAPPED_AUTHOR_RE.fullmatch(author)
                value = next((group for group in match.groups() if group is not None), None) if match else None
                author = value.strip() if value is not None else author
                match = AUTHOR_MISSING_BOOK_OPEN_RE.fullmatch(author)
                if match:
                    author = f"《{match.group(1)}》{match.group(2)}"
                match = AUTHOR_PUBLISHER_CREDIT_RE.fullmatch(author)
                if match:
                    author = match.group(1).strip()
                author = AUTHOR_REVIEW_NOTE_RE.sub("", author)
                author = author.replace("？", "/").rstrip("、，；：/").strip()
                if (len(author) > 80 or AUTHOR_PLACEHOLDER_RE.search(author)
                        or AUTHOR_TITLE_PREFIX_RE.search(author)
                        or AUTHOR_DATE_STATEMENT_RE.search(author)
                        or AUTHOR_PURE_NOISE_RE.fullmatch(author)
                        or AUTHOR_OCR_RESIDUE_RE.fullmatch(author)
                        or AUTHOR_MISPARSED_PROSE_RE.search(author)
                        or any(author.count(opening) != author.count(closing)
                               for opening, closing in AUTHOR_DELIMITER_PAIRS)):
                    continue
                if author:
                    normalized.append(author)
            cleaned["authors"] = normalized
        return cleaned
    return value


def has_article_content(article) -> bool:
    if not isinstance(article, dict):
        return False
    if str(article.get("description") or "").strip():
        return True
    comments = article.get("comments")
    if isinstance(comments, list) and any(str(value or "").strip() for value in comments):
        return True
    parts = article.get("parts")
    return isinstance(parts, list) and any(
        isinstance(part, dict) and str(part.get("text") or "").strip()
        for part in parts
    )


def has_replacement_character(value) -> bool:
    if isinstance(value, str):
        return "\ufffd" in value
    if isinstance(value, list):
        return any(has_replacement_character(item) for item in value)
    if isinstance(value, dict):
        return any(has_replacement_character(item) for item in value.values())
    return False


def article_id(article: dict) -> str:
    dates = sorted(
        f"{date.get('year') or '0000'}-{int(date.get('month') or 0):02d}-{int(date.get('day') or 0):02d}"
        for date in article.get("dates") or [] if isinstance(date, dict)
    )
    value = json.dumps([
        str(article.get("title") or ""), dates, bool(article.get("is_range_date")),
        sorted(str(author) for author in article.get("authors") or []), str(article.get("file_id") or ""),
    ], ensure_ascii=False, separators=(",", ":"))
    return hashlib.md5(value.encode()).hexdigest()[:10]


def apply_metadata_overrides(parsed: Path, config: Path) -> None:
    root = config / "metadata_overrides"
    if not root.exists():
        return
    for override_path in sorted(root.glob("*/*.json")):
        publication_id = override_path.parent.name
        old_article_id = override_path.stem
        override = json.loads(override_path.read_text(encoding="utf-8"))
        if (
            not isinstance(override, dict) or override.get("version") != 1
            or override.get("publication_id") != publication_id
            or override.get("article_id") != old_article_id
            or not isinstance(override.get("new_article_id"), str)
            or not isinstance(override.get("article"), dict)
            or not isinstance(override.get("metadata"), dict)
            or set(override["metadata"]) - {"title", "authors", "dates", "tags"}
        ):
            raise RuntimeError(f"invalid article metadata override: {override_path}")
        matches = []
        for metadata_path in parsed.glob("*/*/*.metadata"):
            if metadata_path.stem == publication_id:
                matches.extend(metadata_path.parent.glob(f"*/{old_article_id}.json"))
        if len(matches) != 1:
            raise RuntimeError(
                f"article metadata override expected one parsed article, found {len(matches)}: {override_path}"
            )
        source = matches[0]
        article = json.loads(source.read_text(encoding="utf-8"))
        identity_fields = ("title", "authors", "dates", "is_range_date")
        if not isinstance(article, dict) or any(
            article.get(key, False if key == "is_range_date" else []) != override["article"].get(
                key, False if key == "is_range_date" else []
            )
            for key in identity_fields
        ):
            raise RuntimeError(f"article metadata override source identity changed: {override_path}")
        article.update({key: value for key, value in override["metadata"].items() if key != "tags"})
        new_article_id = article_id(article)
        if new_article_id != override["new_article_id"]:
            raise RuntimeError(f"article metadata override target identity changed: {override_path}")
        target = source.parent.parent / new_article_id[:3] / f"{new_article_id}.json"
        target_tags = target.with_suffix(".tags")
        source_tags = source.with_suffix(".tags")
        if target != source and (target.exists() or target_tags.exists()):
            raise RuntimeError(f"article metadata override target already exists: {override_path}")
        source.write_text(json.dumps(article, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        if "tags" in override["metadata"]:
            source_tags.write_text(
                json.dumps(override["metadata"]["tags"], ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        if target != source:
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)
            if source_tags.exists():
                source_tags.rename(target_tags)


def clean_selected_archive_parsed(parsed: Path, archive_id: int) -> None:
    if archive_id not in CLEANUP_ARCHIVE_IDS:
        return
    corrupt_articles_removed = 0
    for article_path in parsed.rglob("*.json"):
        article = json.loads(article_path.read_text(encoding="utf-8"))
        cleaned = clean_legacy_image_markup(article)
        if archive_id == 24 and has_replacement_character(cleaned):
            article_path.unlink()
            article_path.with_suffix(".tags").unlink(missing_ok=True)
            corrupt_articles_removed += 1
            continue
        if archive_id in DROP_EMPTY_CONTENT_ARCHIVE_IDS and not has_article_content(cleaned):
            article_path.unlink()
            article_path.with_suffix(".tags").unlink(missing_ok=True)
            continue
        if archive_id == 9 and isinstance(cleaned, dict) and isinstance(cleaned.get("authors"), list):
            authors = []
            for author in cleaned["authors"]:
                if author == "××":
                    continue
                if author == "贵州省委工作组?":
                    author = "贵州省委工作组"
                if author == "—毛远新给毛泽东的报告":
                    author = "毛远新给毛泽东的报告"
                if author == ARCHIVE9_JOINED_CREDIT:
                    authors.append(author.replace("/", "、"))
                    continue
                authors.extend(part.strip() for part in re.split(r"[/；;]", author) if part.strip())
            cleaned["authors"] = authors
        elif archive_id == 24 and isinstance(cleaned, dict) and isinstance(cleaned.get("authors"), list):
            cleaned["authors"] = [
                part.strip()
                for author in cleaned["authors"]
                for part in author.split("/")
                if part.strip()
            ]
        article_path.write_text(
            json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
    if corrupt_articles_removed:
        print(f"archive {archive_id}: removed {corrupt_articles_removed} articles containing U+FFFD text")


def prepare_patch_input(source: Path, target: Path, archive_id: int) -> Path:
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(".git"))
    legacy = target / f"archives{archive_id}"
    if not legacy.exists():
        return target
    for path in legacy.iterdir():
        if not path.is_file() or path.suffix != ".ts":
            raise RuntimeError(f"unexpected legacy OCR patch path: {path}")
        destination = target / path.name
        if destination.exists() and destination.read_bytes() != path.read_bytes():
            raise RuntimeError(f"legacy OCR patch conflicts with root patch: {path.name}")
        if not destination.exists():
            shutil.copy2(path, destination)
    shutil.rmtree(legacy)
    return target


def map_override_patches(patch_input: Path, config: Path) -> None:
    root = config / "metadata_overrides"
    if not root.exists():
        return
    for override_path in sorted(root.glob("*/*.json")):
        override = json.loads(override_path.read_text(encoding="utf-8"))
        publication_id = override_path.parent.name
        old_article_id = override_path.stem
        new_article_id = str(override.get("new_article_id") or "") if isinstance(override, dict) else ""
        if not re.fullmatch(r"[0-9a-f]{10}", new_article_id):
            raise RuntimeError(f"invalid article metadata override target: {override_path}")
        new_patch = patch_input / f"[{new_article_id}][{publication_id}].ts"
        if new_patch.exists():
            shutil.copy2(new_patch, patch_input / f"[{old_article_id}][{publication_id}].ts")


def parsed_tree_changed(parsed: Path, current_commit: str, env: dict[str, str]) -> bool:
    generated_tree = run_output(["git", "write-tree"], cwd=parsed, env=env)
    current_tree = run_output(["git", "rev-parse", f"{current_commit}^{{tree}}"], cwd=parsed, env=env)
    return generated_tree != current_tree


def build_archive(token: str, helper: Path, root: Path, archive_id: int, revisions: dict[str, str]) -> bool:
    repo = f"{REPOSITORY_PREFIX}{archive_id}"
    repo_url = f"https://github.com/{MIRROR_OWNER}/{repo}.git"
    archive_root = root / repo
    archive_root.mkdir()
    env = git_environment(token)
    paths = {}
    for branch in (*INPUT_BRANCHES, PARSED_BRANCH):
        path = archive_root / branch
        clone_branch(repo_url, branch, path, env, revisions[branch])
        paths[branch] = path

    parsed = paths[PARSED_BRANCH]
    patch_input = prepare_patch_input(paths["ocr_patch"], archive_root / "ocr_patch-input", archive_id)
    map_override_patches(patch_input, paths["config"])
    run(["git", "checkout", "--orphan", "parsed-build"], cwd=parsed, env=env)
    run(["git", "reset", "--hard"], cwd=parsed, env=env)
    run_in_container(root, helper, [
        "npm", "run", "build_parsed", "--",
        str(paths["config"]), str(paths["ocr_cache"]), str(patch_input),
        str(parsed), str(paths["main"]),
    ])
    clean_selected_archive_parsed(parsed, archive_id)
    apply_metadata_overrides(parsed, paths["config"])
    sanitize_repository(parsed)
    secure_home = archive_root / "push-home"
    secure_home.mkdir()
    safe_env = clean_environment()
    safe_env["HOME"] = str(secure_home)
    run(["git", "add", "-A"], cwd=parsed, env=safe_env)
    if not parsed_tree_changed(parsed, revisions[PARSED_BRANCH], safe_env):
        if ref_revision(token, MIRROR_OWNER, repo, PARSED_BRANCH) != revisions[PARSED_BRANCH]:
            raise RuntimeError(f"{MIRROR_OWNER}/{repo}:parsed changed during no-op generation; retry the workflow")
        return False
    run(["git", "config", "user.name", "github-actions[bot]"], cwd=parsed, env=safe_env)
    run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], cwd=parsed, env=safe_env)
    run(["git", "commit", "-m", "Rebuild parsed data from anftm corrections"], cwd=parsed, env=safe_env)
    run([
        "git", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=", "-c", "http.proxy=",
        "push", repo_url, "HEAD:parsed",
        f"--force-with-lease=refs/heads/parsed:{revisions[PARSED_BRANCH]}",
    ], cwd=parsed, env=push_environment(token, secure_home))
    return True


def sync_parsed(token: str, root: Path, archive_id: int, mirror: dict[str, str], upstream: dict[str, str]) -> None:
    repo = f"{REPOSITORY_PREFIX}{archive_id}"
    mirror_url = f"https://github.com/{MIRROR_OWNER}/{repo}.git"
    upstream_url = f"https://github.com/{UPSTREAM_OWNER}/{repo}.git"
    target = root / f"{repo}-parsed-sync"
    env = git_environment(token)
    clone_branch(mirror_url, PARSED_BRANCH, target, env, mirror[PARSED_BRANCH])
    run(["git", "fetch", "--depth", "1", upstream_url, PARSED_BRANCH], cwd=target, env=env)
    fetched = subprocess.run(
        ["git", "rev-parse", "FETCH_HEAD"], cwd=str(target), env=env, check=True, capture_output=True, text=True,
    ).stdout.strip()
    if fetched != upstream[PARSED_BRANCH]:
        raise RuntimeError(f"{UPSTREAM_OWNER}/{repo}:parsed changed while synchronizing; retry the workflow")
    secure_home = root / f"{repo}-sync-home"
    secure_home.mkdir()
    run([
        "git", "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=", "-c", "http.proxy=",
        "push", mirror_url, "FETCH_HEAD:parsed",
        f"--force-with-lease=refs/heads/parsed:{mirror[PARSED_BRANCH]}",
    ], cwd=target, env=push_environment(token, secure_home))


def output(name: str, value: str) -> None:
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as target:
            target.write(f"{name}={value}\n")
    print(f"{name}={value}")


def main() -> None:
    token = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("GH_PAT is required")
    state = load_state()
    archives = dict(state["archives"])
    helper_revision = ref_revision(token, "banned-historical-archives", "ocr_helper", "main")
    selected = selected_archive_ids()
    current = {}
    for archive_id in selected:
        repo = f"{REPOSITORY_PREFIX}{archive_id}"
        current[archive_id] = branch_revisions(token, MIRROR_OWNER, repo)

    changed = [
        archive_id for archive_id in selected
        if FORCE_REBUILD or archives.get(str(archive_id)) != source_snapshot(current[archive_id], helper_revision)
    ]
    built = []
    synced = []
    if changed:
        upstreams = {}
        conflicts = []
        for archive_id in changed:
            repo = f"{REPOSITORY_PREFIX}{archive_id}"
            upstream = branch_revisions(token, UPSTREAM_OWNER, repo)
            upstreams[archive_id] = upstream
            if FORCE_REBUILD or needs_local_build(current[archive_id], upstream):
                conflict = ocr_patch_rebase_conflict(
                    token, archive_id, archives.get(str(archive_id)), current[archive_id], upstream,
                )
                if conflict:
                    conflicts.append(conflict)
        if conflicts:
            with tempfile.TemporaryDirectory(prefix="bha-ocr-conflicts-") as temporary:
                root = Path(temporary)
                helper = prepare_helper(root, git_environment(token), helper_revision)
                for conflict in conflicts:
                    archive_id = int(conflict["archive_id"])
                    build_ocr_conflict_previews(
                        token, helper, root, conflict, current[archive_id], upstreams[archive_id],
                    )
            write_ocr_conflicts(conflicts)
            archive_list = ", ".join(str(item["archive_id"]) for item in conflicts)
            raise RuntimeError(
                f"OCR baseline update conflicts with local proofreading in archives {archive_list}; "
                f"see {OCR_CONFLICT_PATH}, then rerun reviewed archives with ALLOW_OCR_PATCH_REBASE=true"
            )
        with tempfile.TemporaryDirectory(prefix="bha-parsed-") as temporary:
            root = Path(temporary)
            helper = None
            for archive_id in changed:
                repo = f"{REPOSITORY_PREFIX}{archive_id}"
                upstream = upstreams[archive_id]
                if FORCE_REBUILD or needs_local_build(current[archive_id], upstream):
                    helper = helper or prepare_helper(root, git_environment(token), helper_revision)
                    build_archive(token, helper, root, archive_id, current[archive_id])
                    built.append(archive_id)
                elif current[archive_id][PARSED_BRANCH] != upstream[PARSED_BRANCH]:
                    latest = branch_revisions(token, MIRROR_OWNER, repo)
                    if latest != current[archive_id]:
                        raise RuntimeError(f"{MIRROR_OWNER}/{repo} changed before parsed synchronization; retry the workflow")
                    sync_parsed(token, root, archive_id, current[archive_id], upstream)
                    synced.append(archive_id)
                archives[str(archive_id)] = source_snapshot(current[archive_id], helper_revision)

    candidate = {"version": 1, "archives": archives}
    CANDIDATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CANDIDATE_PATH.write_text(json.dumps(candidate, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output("state_changed", str(candidate != state).lower())
    output("changed_archives", ",".join(map(str, changed)))
    output("built_archives", ",".join(map(str, built)))
    output("synced_archives", ",".join(map(str, synced)))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
