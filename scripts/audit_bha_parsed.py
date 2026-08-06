#!/usr/bin/env python3
"""Audit BHA parsed branches without extracting their many small files."""

import argparse
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from typing import Any


HTML_TAG_RE = re.compile(
    r"<\s*/?\s*(?:a|b|blockquote|body|br|div|em|font|h[1-6]|head|hr|html|i|img|li|ol|p|span|strong|sub|sup|table|tbody|td|th|thead|title|tr|u|ul)\b[^>]*>",
    re.IGNORECASE,
)
HTML_ENTITY_RE = re.compile(r"&(?:#[0-9]+|#x[0-9a-f]+|[a-z][a-z0-9]+);", re.IGNORECASE)
ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
MOJIBAKE_RE = re.compile(r"(?:锟斤拷|烫烫烫|Ã[\x80-\u00bf]|Â[\x80-\u00bf]|â(?:€|€™|€œ|€œ|€“|€”|€¦)|ðŸ)")
WRAPPED_AUTHOR_RE = re.compile(r"^\s*(?:\(.+\)|（.+）)\s*$")
AUTHOR_IMAGE_RE = re.compile(r"^\s*(?:<\s*[,，]?\s*)?img\s*=\s*[0-9a-z]+>?\s*$", re.IGNORECASE)
OCR_MARKUP_RE = re.compile(r"〖-?[A-Za-z]{2}[/；;][^〗]{1,80}〗")
OCR_MARKUP_UNCLOSED_RE = re.compile(r"〖-?[A-Za-z]{2}[/；;][^〗\r\n]{1,80}$")
TEXT_RULES = {
    "html_tag": HTML_TAG_RE,
    "html_entity": HTML_ENTITY_RE,
    "zero_width": ZERO_WIDTH_RE,
    "replacement_character": re.compile("\ufffd"),
    "control_character": CONTROL_RE,
    "probable_mojibake": MOJIBAKE_RE,
    "ocr_markup": OCR_MARKUP_RE,
    "ocr_markup_unclosed": OCR_MARKUP_UNCLOSED_RE,
}


def parse_archives(value: str) -> list[int]:
    if value == "all":
        return list(range(32))
    selected = set()
    for item in value.split(","):
        if "-" in item:
            start, end = (int(part) for part in item.split("-", 1))
            selected.update(range(start, end + 1))
        else:
            selected.add(int(item))
    if not selected or min(selected) < 0 or max(selected) > 31:
        raise argparse.ArgumentTypeError("archives must be all or numbers from 0 through 31")
    return sorted(selected)


def request_json(url: str, token: str = "") -> dict[str, Any]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "anftm-pipeline-bha-audit/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                value = json.load(response)
            break
        except urllib.error.HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 3:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 3:
                raise
        time.sleep(2 ** attempt)
    if not isinstance(value, dict):
        raise RuntimeError(f"unexpected response from {url}")
    return value


def parsed_commit(owner: str, archive_id: int, token: str = "") -> str:
    if not token:
        command = [
            "git", "ls-remote",
            f"https://github.com/{owner}/banned-historical-archives{archive_id}.git",
            "refs/heads/parsed",
        ]
        for attempt in range(4):
            try:
                result = subprocess.run(
                    command, check=True, capture_output=True, text=True, timeout=60,
                )
                break
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                if attempt == 3:
                    raise RuntimeError(f"archive {archive_id} parsed commit lookup failed")
                time.sleep(2 ** attempt)
        commit = result.stdout.split(maxsplit=1)[0] if result.stdout.strip() else ""
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise RuntimeError(f"archive {archive_id} returned an invalid parsed commit")
        return commit
    repo = urllib.parse.quote(f"banned-historical-archives{archive_id}", safe="")
    owner = urllib.parse.quote(owner, safe="")
    data = request_json(f"https://api.github.com/repos/{owner}/{repo}/git/ref/heads/parsed", token)
    commit = str(data.get("object", {}).get("sha") or "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError(f"archive {archive_id} returned an invalid parsed commit")
    return commit


def download_snapshot(owner: str, archive_id: int, commit: str, cache_dir: Path | None) -> Path:
    filename = f"archives{archive_id}-{commit}.tar.gz"
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        target = cache_dir / filename
        if target.is_file() and target.stat().st_size:
            return target
    else:
        descriptor, name = tempfile.mkstemp(prefix=f"bha-{archive_id}-", suffix=".tar.gz")
        os.close(descriptor)
        target = Path(name)
    url = f"https://codeload.github.com/{owner}/banned-historical-archives{archive_id}/tar.gz/{commit}"
    temporary = target.with_suffix(target.suffix + ".partial")
    request = urllib.request.Request(url, headers={"User-Agent": "anftm-pipeline-bha-audit/1.0"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=300) as response, temporary.open("wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
            temporary.replace(target)
            break
        except urllib.error.HTTPError as exc:
            temporary.unlink(missing_ok=True)
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 3:
                if cache_dir is None:
                    target.unlink(missing_ok=True)
                raise
        except (urllib.error.URLError, TimeoutError):
            temporary.unlink(missing_ok=True)
            if attempt == 3:
                if cache_dir is None:
                    target.unlink(missing_ok=True)
                raise
        time.sleep(2 ** attempt)
    return target


def relative_member_path(name: str) -> PurePosixPath | None:
    path = PurePosixPath(name)
    if len(path.parts) < 2 or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return PurePosixPath(*path.parts[1:])


class Findings:
    def __init__(self, sample_limit: int):
        self.counts: Counter[str] = Counter()
        self.samples: dict[str, list[dict[str, str]]] = defaultdict(list)
        self.sample_limit = sample_limit

    def add(self, issue: str, path: str, field: str = "", value: str = "") -> None:
        self.counts[issue] += 1
        samples = self.samples[issue]
        if len(samples) >= self.sample_limit:
            return
        compact = re.sub(r"\s+", " ", value).strip()
        samples.append({"path": path, "field": field, "excerpt": compact[:160]})


def walk_strings(value: Any, field: str = "$"):
    if isinstance(value, str):
        yield field, value
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from walk_strings(item, f"{field}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from walk_strings(item, f"{field}.{key}")


def audit_text(value: Any, path: str, findings: Findings) -> None:
    matched: set[str] = set()
    for field, text in walk_strings(value):
        for issue, pattern in TEXT_RULES.items():
            match = pattern.search(text) if issue not in matched else None
            if match:
                start = max(0, match.start() - 70)
                end = min(len(text), match.end() + 70)
                findings.add(issue, path, field, text[start:end])
                matched.add(issue)


def valid_content(article: dict[str, Any]) -> bool:
    if str(article.get("description") or "").strip():
        return True
    if any(str(value).strip() for value in article.get("comments", []) if value is not None):
        return True
    return any(
        isinstance(part, dict) and str(part.get("text") or "").strip()
        for part in article.get("parts", [])
    )


def audit_article(article: Any, path: str, findings: Findings) -> None:
    if not isinstance(article, dict):
        findings.add("article_not_object", path)
        return
    audit_text(article, path, findings)
    if not str(article.get("title") or "").strip():
        findings.add("empty_title", path)
    if not valid_content(article):
        findings.add("empty_content", path)
    authors = article.get("authors")
    if authors is not None and not isinstance(authors, list):
        findings.add("authors_not_list", path, "$.authors", str(authors))
    elif isinstance(authors, list):
        author_issues: set[str] = set()
        for index, author in enumerate(authors):
            if not isinstance(author, str):
                continue
            if "author_image_placeholder" not in author_issues and AUTHOR_IMAGE_RE.fullmatch(author):
                findings.add("author_image_placeholder", path, f"$.authors[{index}]", author)
                author_issues.add("author_image_placeholder")
            if "unbalanced_author_parenthesis" not in author_issues and (
                    author.count("(") != author.count(")") or author.count("（") != author.count("）")):
                findings.add("unbalanced_author_parenthesis", path, f"$.authors[{index}]", author)
                author_issues.add("unbalanced_author_parenthesis")
            if "square_bracket_author" not in author_issues and any(char in author for char in "[]［］"):
                findings.add("square_bracket_author", path, f"$.authors[{index}]", author)
                author_issues.add("square_bracket_author")
            if "wrapped_author" not in author_issues and WRAPPED_AUTHOR_RE.fullmatch(author):
                findings.add("wrapped_author", path, f"$.authors[{index}]", author)
                author_issues.add("wrapped_author")
    dates = article.get("dates")
    if dates is not None and not isinstance(dates, list):
        findings.add("dates_not_list", path, "$.dates", str(dates))
    elif isinstance(dates, list):
        for index, date in enumerate(dates):
            if not isinstance(date, dict):
                findings.add("invalid_date", path, f"$.dates[{index}]", str(date))
                break
            year, month, day = date.get("year"), date.get("month"), date.get("day")
            if ((year is not None and not isinstance(year, int))
                    or (month is not None and (not isinstance(month, int) or not 1 <= month <= 12))
                    or (day is not None and (not isinstance(day, int) or not 1 <= day <= 31))):
                findings.add("invalid_date", path, f"$.dates[{index}]", str(date))
                break


def load_json_member(archive: tarfile.TarFile, member: tarfile.TarInfo, path: str, findings: Findings):
    source = archive.extractfile(member)
    if source is None:
        findings.add("unreadable_file", path)
        return None
    try:
        return json.load(source)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        findings.add("invalid_json", path, value=str(exc))
        return None


def audit_snapshot(snapshot: Path, archive_id: int, commit: str, sample_limit: int = 5) -> dict[str, Any]:
    findings = Findings(sample_limit)
    metadata: dict[PurePosixPath, list[str]] = defaultdict(list)
    articles: dict[PurePosixPath, list[tuple[str, str]]] = defaultdict(list)
    tag_files = 0
    with tarfile.open(snapshot, "r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            relative = relative_member_path(member.name)
            if relative is None:
                continue
            path = relative.as_posix()
            if relative.suffix == ".metadata" and len(relative.parts) == 3:
                value = load_json_member(archive, member, path, findings)
                if value is not None:
                    if not isinstance(value, dict):
                        findings.add("metadata_not_object", path)
                    else:
                        audit_text(value, path, findings)
                        metadata[relative.parent].append(relative.stem)
            elif relative.suffix == ".json" and len(relative.parts) >= 4:
                value = load_json_member(archive, member, path, findings)
                if value is not None:
                    audit_article(value, path, findings)
                    articles[PurePosixPath(*relative.parts[:2])].append((relative.stem, path))
            elif relative.suffix == ".tags" and len(relative.parts) >= 4:
                tag_files += 1
                value = load_json_member(archive, member, path, findings)
                if value is not None:
                    audit_text(value, path, findings)
                    if not isinstance(value, list):
                        findings.add("tags_not_list", path)

    all_roots = set(metadata) | set(articles)
    indexed_documents = 0
    unique_doc_ids: dict[str, str] = {}
    duplicate_ids: dict[str, list[str]] = defaultdict(list)
    for root in sorted(all_roots):
        publication_ids = metadata.get(root, [])
        root_articles = articles.get(root, [])
        if not publication_ids:
            for _, path in root_articles:
                findings.add("orphan_article", path)
            continue
        if len(publication_ids) > 1:
            findings.add("multiple_metadata", root.as_posix(), value=",".join(publication_ids))
        for publication_id in publication_ids:
            for article_id, path in root_articles:
                indexed_documents += 1
                doc_id = f"{archive_id}:{len(article_id)}:{article_id}:{publication_id}"
                previous = unique_doc_ids.setdefault(doc_id, path)
                if previous != path:
                    if not duplicate_ids[doc_id]:
                        duplicate_ids[doc_id].append(previous)
                    duplicate_ids[doc_id].append(path)
    for doc_id, paths in duplicate_ids.items():
        findings.add("duplicate_doc_id", paths[-1], "doc_id", f"{doc_id}: {', '.join(paths)}")

    return {
        "archive_id": archive_id,
        "commit": commit,
        "article_files": sum(len(value) for value in articles.values()),
        "tag_files": tag_files,
        "metadata_files": sum(len(value) for value in metadata.values()),
        "indexed_documents": indexed_documents,
        "unique_doc_ids": len(unique_doc_ids),
        "overwritten_documents": indexed_documents - len(unique_doc_ids),
        "findings": dict(sorted(findings.counts.items())),
        "samples": dict(sorted(findings.samples.items())),
    }


def audit_remote(owner: str, archive_id: int, token: str, cache_dir: Path | None, sample_limit: int):
    commit = parsed_commit(owner, archive_id, token)
    snapshot = download_snapshot(owner, archive_id, commit, cache_dir)
    try:
        return audit_snapshot(snapshot, archive_id, commit, sample_limit)
    finally:
        if cache_dir is None:
            snapshot.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archives", default="all", help="all, comma-separated IDs, or ranges")
    parser.add_argument("--owner", default=os.environ.get("BHA_ARCHIVE_OWNER", "anftm"))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--sample-limit", type=int, default=5)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    archive_ids = parse_archives(args.archives)
    if args.workers < 1 or args.sample_limit < 0:
        parser.error("workers must be positive and sample-limit must not be negative")
    token = os.environ.get("GH_PAT") or os.environ.get("GITHUB_TOKEN", "")
    started = time.time()
    reports = []
    failures = []
    with ThreadPoolExecutor(max_workers=min(args.workers, len(archive_ids))) as executor:
        futures = {
            executor.submit(audit_remote, args.owner, archive_id, token, args.cache_dir, args.sample_limit): archive_id
            for archive_id in archive_ids
        }
        for future in as_completed(futures):
            try:
                report = future.result()
            except Exception as exc:
                archive_id = futures[future]
                failures.append({"archive_id": archive_id, "error": str(exc)})
                print(f"archive {archive_id}: failed: {exc}", file=os.sys.stderr)
                continue
            reports.append(report)
            print(
                f"archive {report['archive_id']}: {report['indexed_documents']} documents, "
                f"{sum(report['findings'].values())} findings",
                file=os.sys.stderr,
            )
    reports.sort(key=lambda item: item["archive_id"])
    finding_totals: Counter[str] = Counter()
    for report in reports:
        finding_totals.update(report["findings"])
    result = {
        "owner": args.owner,
        "generated_at": int(time.time()),
        "elapsed_seconds": round(time.time() - started, 3),
        "archive_count": len(reports),
        "failed_archive_count": len(failures),
        "failures": sorted(failures, key=lambda item: item["archive_id"]),
        "article_files": sum(item["article_files"] for item in reports),
        "indexed_documents": sum(item["indexed_documents"] for item in reports),
        "unique_doc_ids": sum(item["unique_doc_ids"] for item in reports),
        "overwritten_documents": sum(item["overwritten_documents"] for item in reports),
        "finding_totals": dict(sorted(finding_totals.items())),
        "archives": reports,
    }
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
