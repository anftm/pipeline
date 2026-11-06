import hashlib
import json
import os
import re
import shutil
import subprocess
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import TypedDict, cast
import jieba
from .config import ARCHIVE_END, ARCHIVE_START, PARSED_ROOT, REPO_PREFIX

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
SEARCH_RUN_RE = re.compile(r"[a-z0-9]+|[\u3400-\u4dbf\u4e00-\u9fff]+", re.IGNORECASE)
DOCUMENT_BUILD_VERSION = "1"
PARSED_CACHE_ROOT = Path(os.environ["BHA_PARSED_CACHE_ROOT"]) if os.environ.get("BHA_PARSED_CACHE_ROOT") else None
def remote_git_timeout() -> int:
    try:
        return max(1, int(os.environ.get("BHA_REMOTE_GIT_TIMEOUT_SECONDS", "30")))
    except ValueError:
        return 30


REMOTE_GIT_TIMEOUT_SECONDS = remote_git_timeout()
REMOTE_GIT_WORKERS = 8


class SearchDocument(TypedDict):
    doc_id: str
    article_id: str
    publication_id: str
    publication_name: str
    publication_author: str
    publication_type: str
    source_files: list[str]
    archive_id: int
    title: str
    title_words: str
    authors: list[str]
    authors_words: str
    date_min: int | None
    date_max: int | None
    date_display: list[str]
    dates: list[int]
    date_years: list[int]
    date_months: list[int]
    date_days: list[int]
    date_month_days: list[str]
    date_year_months: list[str]
    date_year_days: list[str]
    date_year_month_days: list[str]
    tag_names: list[str]
    tag_names_words: str
    tag_types: list[str]
    publication_name_words: str
    content: str
    path: str
    article: dict[str, object]


def object_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    raw = cast(dict[object, object], value)
    if any(not isinstance(key, str) for key in raw):
        return None
    return cast(dict[str, object], raw)


def object_list(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []

def initialize_search_tokenizer() -> None:
    jieba.initialize()

def run(command: list[str], cwd: Path | None = None) -> None:
    subprocess.run(command, cwd=str(cwd) if cwd else None, check=True)

def link_or_copy(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)

def cache_parsed_repository(source: Path, archive_id: int) -> None:
    if PARSED_CACHE_ROOT is None:
        return
    PARSED_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    target = PARSED_CACHE_ROOT / f"archives{archive_id}"
    if target.is_dir() and (target / ".git").is_dir():
        source_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, capture_output=True, text=True, check=True).stdout.strip()
        cached_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=target, capture_output=True, text=True, check=True).stdout.strip()
        if source_head == cached_head:
            return
    temporary = PARSED_CACHE_ROOT / f".archives{archive_id}.{os.getpid()}.tmp"
    shutil.rmtree(temporary, ignore_errors=True)
    shutil.copytree(source, temporary, copy_function=link_or_copy)
    shutil.rmtree(target, ignore_errors=True)
    temporary.replace(target)

def ensure_parsed_data(
    progress: Callable[[int, int], None] | None = None,
    expected_commits: dict[int, str] | None = None,
    archive_ids: Iterable[int] | None = None,
) -> dict[int, set[str]] | None:
    PARSED_ROOT.mkdir(parents=True, exist_ok=True)
    archive_ids = list(archive_ids) if archive_ids is not None else list(range(ARCHIVE_START, ARCHIVE_END + 1))
    total = len(archive_ids)
    changed_paths: dict[int, set[str]] | None = {} if expected_commits is not None else None
    for position, archive_id in enumerate(archive_ids, start=1):
        if progress:
            progress(position, total)
        target = PARSED_ROOT / f"archives{archive_id}"
        repo = f"{REPO_PREFIX}/banned-historical-archives{archive_id}.git"
        if (target / ".git").exists():
            run(["git", "remote", "set-url", "origin", repo], target)
            expected = expected_commits.get(archive_id) if expected_commits else None
            current = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=target, check=True, capture_output=True, text=True,
            ).stdout.strip()
            if expected and current == expected:
                if changed_paths is not None:
                    changed_paths[archive_id] = set()
                cache_parsed_repository(target, archive_id)
                continue
            run(["git", "fetch", "--depth", "1", "origin", expected or "parsed"], target)
            if expected:
                diff = subprocess.run(
                    ["git", "diff", "--name-only", current, "FETCH_HEAD"],
                    cwd=target, check=True, capture_output=True, text=True,
                ).stdout.splitlines()
                if changed_paths is not None:
                    changed_paths[archive_id] = {path for path in diff if path}
            run(["git", "clean", "-fdx"], target)
            run(["git", "checkout", "parsed"], target)
            run(["git", "reset", "--hard", "FETCH_HEAD"], target)
            if expected and subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=target, check=True, capture_output=True, text=True,
            ).stdout.strip() != expected:
                raise RuntimeError(f"parsed commit mismatch for archive {archive_id}")
            cache_parsed_repository(target, archive_id)
            continue
        if target.exists():
            shutil.rmtree(target)
        cached = PARSED_CACHE_ROOT / f"archives{archive_id}" if PARSED_CACHE_ROOT is not None else None
        if cached is not None and cached.is_dir() and (cached / ".git").exists():
            shutil.copytree(cached, target, copy_function=link_or_copy)
        else:
            run(["git", "clone", "--depth", "1", "--branch", "parsed", repo, str(target)])
        expected = expected_commits.get(archive_id) if expected_commits else None
        if expected and subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=target, check=True, capture_output=True, text=True,
        ).stdout.strip() != expected:
            run(["git", "fetch", "--depth", "1", "origin", expected], target)
            run(["git", "reset", "--hard", "FETCH_HEAD"], target)
        cache_parsed_repository(target, archive_id)
    return changed_paths

def parsed_corpus_fingerprint(
    remote: bool = False, revisions: dict[int, str] | None = None
) -> str:
    if remote:
        raise RuntimeError("remote parsed tree fingerprints require checked-out data")
    revisions = revisions or parsed_archive_revisions(remote=remote)
    return hashlib.sha256(
        "\n".join(f"{archive_id}:{revisions[archive_id]}" for archive_id in sorted(revisions)).encode()
    ).hexdigest()

def parsed_archive_revisions(remote: bool = False, archive_ids: Iterable[int] | None = None) -> dict[int, str]:
    if remote:
        raise RuntimeError("remote parsed tree revisions require checked-out data")
    revisions: dict[int, str] = {}
    selected = archive_ids if archive_ids is not None else range(ARCHIVE_START, ARCHIVE_END + 1)
    for archive_id in selected:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=PARSED_ROOT / f"archives{archive_id}",
            check=True,
            capture_output=True,
            text=True,
        )
        revision = result.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise RuntimeError(f"invalid parsed revision for archive {archive_id}")
        revisions[archive_id] = revision
    return revisions

def parsed_archive_commits(remote: bool = False, archive_ids: Iterable[int] | None = None) -> dict[int, str]:
    revisions: dict[int, str] = {}
    selected = list(archive_ids) if archive_ids is not None else list(range(ARCHIVE_START, ARCHIVE_END + 1))
    if not remote:
        for archive_id in selected:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=PARSED_ROOT / f"archives{archive_id}",
                check=True,
                capture_output=True,
                text=True,
            )
            revision = result.stdout.strip()
            if not re.fullmatch(r"[0-9a-f]{40}", revision):
                raise RuntimeError(f"invalid parsed commit for archive {archive_id}")
            revisions[archive_id] = revision
        return revisions

    def fetch_remote(archive_id: int) -> tuple[int, str]:
        result = subprocess.run(
            ["git", "ls-remote", f"{REPO_PREFIX}/banned-historical-archives{archive_id}.git", "refs/heads/parsed"],
            check=True,
            capture_output=True,
            text=True,
            timeout=REMOTE_GIT_TIMEOUT_SECONDS,
        )
        revision = result.stdout.split(maxsplit=1)[0]
        if not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise RuntimeError(f"invalid parsed commit for archive {archive_id}")
        return archive_id, revision

    with ThreadPoolExecutor(max_workers=min(REMOTE_GIT_WORKERS, max(1, len(selected)))) as executor:
        for archive_id, revision in executor.map(fetch_remote, selected):
            revisions[archive_id] = revision
    return revisions

def parsed_archive_tree_revisions(
    commits: dict[int, str], archive_ids: Iterable[int] | None = None,
) -> dict[int, str]:
    """Read tree SHAs for remote commits without downloading repository data."""
    selected = list(archive_ids) if archive_ids is not None else list(commits)
    parsed = urllib.parse.urlparse(REPO_PREFIX)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.netloc != "github.com" or len(parts) != 1:
        raise RuntimeError("remote tree lookup requires a github.com repository prefix")
    owner = urllib.parse.quote(parts[0], safe="")

    def fetch_tree(archive_id: int) -> tuple[int, str]:
        repo = urllib.parse.quote(f"banned-historical-archives{archive_id}", safe="")
        commit = commits[archive_id]
        url = f"https://api.github.com/repos/{owner}/{repo}/git/commits/{commit}"
        request = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "bha-search-source-check/1.0",
        })
        try:
            with urllib.request.urlopen(request, timeout=REMOTE_GIT_TIMEOUT_SECONDS) as response:
                payload: object = json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"remote tree lookup failed for archive {archive_id}: HTTP {exc.code}") from exc
        payload_object = object_dict(payload) or {}
        tree_object = object_dict(payload_object.get("tree")) or {}
        tree = str(tree_object.get("sha") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", tree):
            raise RuntimeError(f"invalid remote tree revision for archive {archive_id}")
        return archive_id, tree

    with ThreadPoolExecutor(max_workers=min(REMOTE_GIT_WORKERS, max(1, len(selected)))) as executor:
        return dict(executor.map(fetch_tree, selected))

def read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"failed to parse JSON: {path}") from exc

def text_from_article(article: Mapping[str, object]) -> str:
    parts = object_list(article.get("parts"))
    comments = object_list(article.get("comments"))
    values: list[str] = []
    if article.get("description"):
        values.append(str(article.get("description")))
    for raw_part in parts:
        part = object_dict(raw_part)
        if part and part.get("text"):
            values.append(str(part.get("text")))
    for comment in comments:
        if comment:
            values.append(str(comment))
    return "\n".join(values)

@lru_cache(maxsize=512)
def _search_words(text: str) -> str:
    tokens: list[str] = []
    for run in SEARCH_RUN_RE.findall(text):
        if CJK_RE.fullmatch(run):
            tokens.extend(token.strip() for token in jieba.lcut(run) if token.strip())
        else:
            tokens.append(run)
    return " ".join(tokens)


def search_words(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return _search_words(text)

def normalize_date_value(item: Mapping[str, object]) -> int | None:
    year = item.get("year")
    if not isinstance(year, int):
        return None
    raw_month, raw_day = item.get("month"), item.get("day")
    month = raw_month if isinstance(raw_month, int) else 1
    day = raw_day if isinstance(raw_day, int) else 1
    return year * 10000 + month * 100 + day

def normalize_dates(article: Mapping[str, object]) -> tuple[int | None, int | None, list[str], list[int], list[int], list[int], list[int], list[str], list[str], list[str], list[str]]:
    raw_dates = object_list(article.get("dates"))
    values: list[int] = []
    displays: list[str] = []
    years: set[int] = set()
    months: set[int] = set()
    days: set[int] = set()
    month_days: set[str] = set()
    year_months: set[str] = set()
    year_days: set[str] = set()
    year_month_days: set[str] = set()
    for raw_item in raw_dates:
        item = object_dict(raw_item)
        if item is None:
            continue
        value = normalize_date_value(item)
        if value is not None:
            values.append(value)
        year_value = item.get("year")
        month_value = item.get("month")
        day_value = item.get("day")
        if isinstance(year_value, int):
            years.add(year_value)
        if isinstance(month_value, int):
            months.add(month_value)
        if isinstance(day_value, int):
            days.add(day_value)
        if isinstance(month_value, int) and isinstance(day_value, int):
            month_days.add(f"{month_value:02d}-{day_value:02d}")
        if isinstance(year_value, int) and isinstance(month_value, int):
            year_months.add(f"{year_value:04d}-{month_value:02d}")
        if isinstance(year_value, int) and isinstance(day_value, int):
            year_days.add(f"{year_value:04d}-{day_value:02d}")
        if isinstance(year_value, int) and isinstance(month_value, int) and isinstance(day_value, int):
            year_month_days.add(f"{year_value:04d}-{month_value:02d}-{day_value:02d}")
        year = item.get("year") if item.get("year") is not None else "----"
        month = item.get("month") if item.get("month") is not None else "--"
        day = item.get("day") if item.get("day") is not None else "--"
        displays.append(f"{year}/{month}/{day}")
    return (
        min(values) if values else None,
        max(values) if values else None,
        displays,
        sorted(set(values)),
        sorted(years),
        sorted(months),
        sorted(days),
        sorted(month_days),
        sorted(year_months),
        sorted(year_days),
        sorted(year_month_days),
    )

def normalize_tags(tags: object) -> tuple[list[str], list[str]]:
    names: list[str] = []
    types: list[str] = []
    for raw_item in object_list(tags):
        item = object_dict(raw_item)
        if item is None:
            continue
        name = item.get("name")
        tag_type = item.get("type")
        if name is not None and str(name).strip():
            names.append(str(name).strip())
        if tag_type is not None and str(tag_type).strip():
            types.append(str(tag_type).strip())
    return names, types

def iter_documents(
    archive_ids: Iterable[int] | None = None,
    changed_paths: dict[int, set[str]] | None = None,
) -> Iterator[SearchDocument]:
    selected_archives = (
        sorted(set(archive_ids)) if archive_ids is not None else range(ARCHIVE_START, ARCHIVE_END + 1)
    )
    for archive_id in selected_archives:
        archive_root = PARSED_ROOT / f"archives{archive_id}"
        if not archive_root.exists():
            continue
        for metadata_path in archive_root.glob("*/*/*.metadata"):
            publication_id = metadata_path.stem
            publication = object_dict(read_json(metadata_path))
            if publication is None:
                raise ValueError(f"publication metadata must be an object: {metadata_path}")
            publication_name = str(publication.get("name") or publication_id)
            publication_name_words = search_words(publication_name)
            publication_files = object_list(publication.get("files"))
            book_root = metadata_path.parent
            metadata_relative = metadata_path.relative_to(archive_root).as_posix()
            archive_changed = changed_paths.get(archive_id) if changed_paths is not None else None
            for article_path in book_root.glob("*/*.json"):
                if archive_changed is not None:
                    article_relative = article_path.relative_to(archive_root).as_posix()
                    tags_relative = article_path.with_suffix(".tags").relative_to(archive_root).as_posix()
                    if metadata_relative not in archive_changed and article_relative not in archive_changed and tags_relative not in archive_changed:
                        continue
                article_id = article_path.stem
                article = object_dict(read_json(article_path))
                if article is None:
                    raise ValueError(f"article must be an object: {article_path}")
                tags_path = article_path.with_suffix(".tags")
                tags: object = read_json(tags_path) if tags_path.exists() else []
                tag_names, tag_types = normalize_tags(tags)
                authors = [str(item).strip() for item in object_list(article.get("authors")) if str(item).strip()]
                date_min, date_max, date_display, dates, date_years, date_months, date_days, date_month_days, date_year_months, date_year_days, date_year_month_days = normalize_dates(article)
                doc_id = f"{archive_id}:{len(article_id)}:{article_id}:{publication_id}"
                title = str(article.get("title") or article_id)
                content = text_from_article(article)
                yield {
                    "doc_id": doc_id,
                    "article_id": article_id,
                    "publication_id": publication_id,
                    "publication_name": publication_name,
                    "publication_author": str(publication.get("author") or "").strip(),
                    "publication_type": str(publication.get("type") or "").strip(),
                    "source_files": [str(item).strip() for item in publication_files if str(item).strip()],
                    "archive_id": archive_id,
                    "title": title,
                    "title_words": search_words(title),
                    "authors": authors,
                    "authors_words": search_words(" ".join(authors)),
                    "date_min": date_min,
                    "date_max": date_max,
                    "date_display": date_display,
                    "dates": dates,
                    "date_years": date_years,
                    "date_months": date_months,
                    "date_days": date_days,
                    "date_month_days": date_month_days,
                    "date_year_months": date_year_months,
                    "date_year_days": date_year_days,
                    "date_year_month_days": date_year_month_days,
                    "tag_names": tag_names,
                    "tag_names_words": search_words(" ".join(tag_names)),
                    "tag_types": tag_types,
                    "publication_name_words": publication_name_words,
                    "content": content,
                    "path": str(article_path.relative_to(archive_root)),
                    "article": {**article, "tags": tags},
                }

def iter_document_ids(archive_ids: Iterable[int] | None = None) -> Iterator[str]:
    selected_archives = (
        sorted(set(archive_ids)) if archive_ids is not None else range(ARCHIVE_START, ARCHIVE_END + 1)
    )
    for archive_id in selected_archives:
        archive_root = PARSED_ROOT / f"archives{archive_id}"
        if not archive_root.exists():
            continue
        for metadata_path in archive_root.glob("*/*/*.metadata"):
            publication_id = metadata_path.stem
            for article_path in metadata_path.parent.glob("*/*.json"):
                article_id = article_path.stem
                yield f"{archive_id}:{len(article_id)}:{article_id}:{publication_id}"
