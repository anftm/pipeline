#!/usr/bin/env python3
"""Plan only Reader stream workers with pending source records."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re

from huggingface_hub import HfFileSystem

try:
    from .reader_assets import bucket_conversion_contract, decode_search_payload, relative_path
    from .reader_stream_index import merge_shards, reusable_entry, shard_for, unchanged_sources, validate_index
    from .reader_bucket_store import retry_read
    from .pilot_media_stream import MEDIA_EXTENSIONS
except ImportError:
    from reader_assets import bucket_conversion_contract, decode_search_payload, relative_path
    from reader_stream_index import merge_shards, reusable_entry, shard_for, unchanged_sources, validate_index
    from reader_bucket_store import retry_read
    from pilot_media_stream import MEDIA_EXTENSIONS


KINDS = {
    "image": {"extensions": ("jpg", "jpeg", "png", "bmp", "webp", "gif", "tif", "tiff"), "sharded": True},
    "media": {"extensions": ("audio", "video", "swf"), "sharded": True},
    "ebook": {"extensions": ("epub", "mobi", "azw3", "fb2", "chm"), "sharded": True},
    "office-chapters": {"extensions": ("doc", "docx", "odt", "rtf"), "sharded": False},
    "converted-ebook": {"extensions": ("mobi", "azw3", "fb2"), "sharded": False},
    "text": {"extensions": ("txt", "md", "markdown", "vcf", "ini"), "sharded": False},
    "office": {"extensions": ("doc", "docx", "odt", "rtf"), "sharded": False},
    "spreadsheet": {"extensions": ("xls", "xlsx", "csv", "ods"), "sharded": False},
    "web": {"extensions": ("html", "htm", "mht", "mhtml"), "sharded": False},
    "static-pdf": {"extensions": ("ppt", "pptx", "pps", "wps", "ps"), "sharded": False},
}

LIBRARY_SCOPE = "VoiceOfML/MLMRL-Library"
ALL_SEARCH_SCOPE = "all-search-repositories"
REBUILD_SHARDS = 8
REBUILD_ATTEMPTS = 200
REBUILD_WORKERS = 4


def args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--kind", choices=KINDS)
    p.add_argument("--extension", action="append", dest="extensions")
    p.add_argument("--media-extension", choices=("all", *sorted(MEDIA_EXTENSIONS)), default="all",
                   help="filter media source extension; --extension still selects audio/video/swf")
    p.add_argument("--search-data", type=Path, default=Path("output/search_data.json"))
    p.add_argument("--revisions", type=Path, default=Path("state/commits.json"))
    p.add_argument("--bucket", default="vomebook/reader-assets-v2")
    p.add_argument("--shard-count", type=int, default=8)
    p.add_argument("--shard-index", type=int, help="plan a single shard only")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--retry-failures", action="store_true")
    p.add_argument("--verify-source-sha", action="store_true")
    p.add_argument("--report", type=Path)
    p.add_argument("--scope", help=f"read-only ebook rebuild scope: {LIBRARY_SCOPE} or {ALL_SEARCH_SCOPE}")
    p.add_argument("--rebuild-plan", type=Path)
    p.add_argument("--search-generation")
    p.add_argument("--source-revision")
    p.add_argument("--reader-generation")
    p.add_argument("--expected-sha")
    return p.parse_args()


def read_json(fs: HfFileSystem, bucket: str, path: str) -> dict:
    def load():
        with fs.open(f"hf://buckets/{bucket}/{path}", "rb") as stream:
            value = json.loads(stream.read().decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"invalid Reader stream index: {bucket}/{path}")
        return validate_index(value, f"{bucket}/{path}")
    try:
        return retry_read(load)
    except FileNotFoundError:
        return {}


def source_extension(record: dict) -> str:
    return str(record.get("Extension") or "").lower().lstrip(".")


def selected_records(path: Path, revisions: dict, kind: str, wanted: set[str],
                     media_extension: str = "all") -> list[dict]:
    if media_extension != "all" and (kind != "media" or media_extension not in MEDIA_EXTENSIONS):
        raise ValueError(f"unsupported media extension for {kind}: {media_extension}")
    records = decode_search_payload(json.loads(path.read_text(encoding="utf-8")))
    result = []
    for record in records:
        ext = source_extension(record)
        repo = str(record.get("Repo") or "")
        rel = relative_path(record)
        revision = str(revisions.get(repo) or "")
        if not repo or not revision:
            continue
        if kind == 'image' and int(record.get('Size') or 0) <= 0:
            # The image worker verifies the pinned source length before upload.
            continue
        if kind == "media":
            if ext not in MEDIA_EXTENSIONS or (media_extension != "all" and ext != media_extension):
                continue
            contract = bucket_conversion_contract(repo, rel, ext, int(record.get("Size") or 0))
            media_kind = contract[1] if contract else ("swf" if ext == "swf" else "")
            # Media worker names are audio/video/swf, while source extensions vary.
            if media_kind not in wanted:
                continue
            ext = media_kind
        elif ext not in wanted:
            continue
        result.append({"key": f"{repo}\0{rel}", "extension": ext, "revision": revision})
    return result


def root_for(kind: str, extension: str) -> str:
    if kind == "image":
        return "pages/image"
    if kind == "media":
        return f"media/{extension}"
    if kind in {"ebook", "converted-ebook"}:
        return f"chapters/ebook/{extension}"
    if kind == "office-chapters":
        # Office sources are normalized to EPUB chapters, so all four source
        # extensions share one canonical index and one GC-visible namespace.
        return "chapters/ebook/epub"
    return {
        "text": "documents/text",
        "office": f"documents/office/{extension}",
        "spreadsheet": f"documents/spreadsheet/{extension}",
        "web": f"documents/web/{extension}",
        "static-pdf": f"documents/pdf/{extension}",
    }[kind]


def completed_entries(index: dict, retry_failures: bool = False,
                      profile: str | None = None) -> dict[str, str]:
    completed = {}
    fields = ("files",) if retry_failures else ("files", "failures")
    for field in fields:
        for entry in index.get(field, []):
            if isinstance(entry, dict) and entry.get("key"):
                if field == "files" and not reusable_entry(entry):
                    continue
                if profile is not None and entry.get("profile") != profile:
                    continue
                completed[entry["key"]] = entry.get("source_revision")
    if retry_failures:
        for failure in index.get('failures', []):
            if profile is None or failure.get('profile') in (None, profile):
                completed.pop(failure['key'], None)
    return completed


def rebuild_preview(a: argparse.Namespace) -> int:
    """Validate the preview handoff, never produce a live-worker matrix."""
    if (a.kind != "ebook" or a.scope not in (LIBRARY_SCOPE, ALL_SEARCH_SCOPE)
            or a.rebuild_plan is None or a.report is None or a.extensions
            or a.shard_index is not None or a.shard_count != REBUILD_SHARDS
            or a.retry_failures or a.verify_source_sha):
        raise ValueError("Ebook preview requires an exact rebuild scope, eight shards and no legacy filters")
    all_search = a.scope == ALL_SEARCH_SCOPE
    pins = {"search_generation": a.search_generation}
    if all_search:
        if a.source_revision is not None:
            raise ValueError("All-Search preview uses the Search generation, not a singular source revision")
    else:
        pins["source_revision"] = a.source_revision
    pins["reader_generation"] = a.reader_generation
    for field, value in pins.items():
        pattern = r"[0-9a-f]{40}" if field == "source_revision" else r"[0-9a-f]{64}"
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise ValueError(f"Ebook preview requires an immutable {field}")
    if not a.expected_sha or not re.fullmatch(r"[0-9a-f]{40}", a.expected_sha):
        raise ValueError("Ebook preview requires a pinned pipeline SHA")
    if len({path.resolve() for path in (a.rebuild_plan, a.output, a.report)}) != 3:
        raise ValueError("Ebook plan, report and matrix paths must be distinct")

    raw = a.rebuild_plan.read_bytes()
    plan = json.loads(raw)
    if (not isinstance(plan, dict) or plan.get("kind") != "reader-ebook-full-rebuild-plan"
            or type(plan.get("version")) is not int or plan["version"] != 1
            or type(plan.get("shards")) is not int or plan["shards"] != REBUILD_SHARDS
            or type(plan.get("max_attempts_per_shard")) is not int or plan["max_attempts_per_shard"] != REBUILD_ATTEMPTS
            or type(plan.get("remote_writes")) is not int or plan["remote_writes"] != 0
            or any(plan.get(field) != value for field, value in pins.items())
            or plan.get("publication_ready", False) is not False
            or plan.get("expected_sha", a.expected_sha) != a.expected_sha):
        raise ValueError("Ebook rebuild plan differs from the read-only preview pins")
    rows = plan.get("rows")
    if all_search:
        revisions, unmapped = plan.get("source_revisions"), plan.get("unmapped")
        if (plan.get("scope") != ALL_SEARCH_SCOPE or "repo" in plan or "source_revision" in plan
                or not isinstance(revisions, dict) or not revisions
                or any(not re.fullmatch(r"VoiceOfML/[A-Za-z0-9._-]+", repo)
                       or not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision)
                       for repo, revision in revisions.items())
                or not isinstance(unmapped, list)):
            raise ValueError("All-Search rebuild plan requires a sealed repository revision map and unmapped rows")
        changed = plan.get("source_heads_changed", [])
        if (not isinstance(changed, list) or any(not isinstance(repo, str) or repo not in revisions for repo in changed)
                or len(set(changed)) != len(changed)):
            raise ValueError("All-Search source head changes must name unique pinned repositories")
    else:
        if plan.get("repo") != LIBRARY_SCOPE:
            raise ValueError("Library rebuild plan differs from the scoped repository")
        revisions, unmapped = {LIBRARY_SCOPE: a.source_revision}, []
    if not isinstance(rows, list) or not (rows or unmapped):
        raise ValueError("Ebook rebuild plan contains no chapter sources")

    seen = set()
    extensions = sorted(KINDS["ebook"]["extensions"])
    counts = {repo: {ext: [0] * REBUILD_SHARDS for ext in extensions} for repo in sorted(revisions)}
    for scheduled, sources in ((True, rows), (False, unmapped)):
        for row in sources:
            if not isinstance(row, dict):
                raise ValueError("invalid ebook rebuild source")
            repo, key, path, ext, shard = (row.get(f) for f in ("repo", "key", "path", "extension", "shard"))
            if (not isinstance(repo, str) or repo not in revisions or row.get("revision") != revisions[repo]
                    or not isinstance(path, str) or "\\" in path or any(ord(c) < 32 or ord(c) == 127 for c in path)
                    or any(part in {"", ".", ".."} for part in path.split("/")) or key != f"{repo}\0{path}"
                    or not isinstance(ext, str) or ext not in counts[repo] or not path.lower().endswith("." + ext)
                    or ((scheduled or "shard" in row)
                        and (type(shard) is not int or shard != shard_for(key, REBUILD_SHARDS)))
                    or key in seen):
                raise ValueError("Ebook rebuild source differs from the scoped plan or shard assignment")
            seen.add(key)
            old_digest, old_manifest = row.get("source_sha256"), row.get("old_manifest")
            size = row.get("bytes")
            if not scheduled:
                if type(size) is not int or size < 0 or old_manifest is not None:
                    raise ValueError("Invalid unmapped ebook rebuild source")
                continue
            if (type(size) is not int or size <= 0
                    or not isinstance(old_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", old_digest)
                    or not isinstance(old_manifest, str) or not re.fullmatch(
                        rf"chapters/ebook/{ext}/{old_digest}/[0-9a-f]{{16}}/chapter-manifest\.json", old_manifest)):
                raise ValueError("Ebook rebuild source lacks a pinned prior chapter route")
            counts[repo][ext][shard] += 1
            if counts[repo][ext][shard] > REBUILD_ATTEMPTS:
                raise ValueError(f"Ebook rebuild exceeds 200 attempts for {repo} {ext} shard {shard}; "
                                 "refusing a partial plan")

    if all_search:
        report_kind = "reader-all-search-repositories-ebook-rebuild-preview"
        totals = {"source_revisions": revisions, "source_heads_changed": changed,
                  "attempts_by_repo_format_and_shard": counts, "unmapped": len(unmapped)}
    else:
        report_kind = "reader-library-ebook-rebuild-preview"
        totals = {"attempts_by_format_and_shard": counts[LIBRARY_SCOPE]}
    report = {"version": 1, "kind": report_kind, "scope": a.scope,
              **pins, "expected_sha": a.expected_sha, "plan_sha256": hashlib.sha256(raw).hexdigest(),
              "shard_count": REBUILD_SHARDS, "max_attempts_per_shard": REBUILD_ATTEMPTS,
              "workers": REBUILD_WORKERS, **totals, "sources": len(rows),
              "remote_writes": 0, "publication_ready": False}
    a.report.parent.mkdir(parents=True, exist_ok=True)
    a.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text('{"include":[]}\n', encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


def main() -> int:
    a = args()
    if any(getattr(a, field, None) is not None for field in
           ("scope", "rebuild_plan", "search_generation", "source_revision", "reader_generation", "expected_sha")):
        return rebuild_preview(a)
    if a.shard_count < 1:
        raise ValueError("shard-count must be positive")
    shard_index = getattr(a, 'shard_index', None)
    if shard_index is not None and (not KINDS[a.kind]['sharded'] or not 0 <= shard_index < a.shard_count):
        raise ValueError('shard-index requires a valid sharded format and shard number')
    config = KINDS[a.kind]
    requested = set(a.extensions or config["extensions"])
    wanted = set(config["extensions"]) if "all" in requested else requested
    invalid = wanted - set(config["extensions"])
    if invalid:
        raise ValueError(f"unsupported extensions: {sorted(invalid)}")
    revisions = json.loads(a.revisions.read_text(encoding="utf-8"))
    records = selected_records(a.search_data, revisions, a.kind, wanted,
                               getattr(a, "media_extension", "all"))
    token = os.environ.get("HF_TOKEN")
    fs = HfFileSystem(token=token)
    chapter_profile = None
    if a.kind in {"ebook", "office-chapters"}:
        try:
            from .pilot_epub_chapters import chapter_profile
        except ImportError:
            from pilot_epub_chapters import chapter_profile
    include = []
    summaries = []
    for extension in sorted(wanted):
        candidates = [item for item in records if item["extension"] == extension]
        expected = chapter_profile(extension) if chapter_profile else None
        if config["sharded"]:
            root = root_for(a.kind, extension)
            if a.kind == 'image':
                try:
                    from .pilot_image_page_stream import shard_baseline
                except ImportError:
                    from pilot_image_page_stream import shard_baseline
                canonical = read_json(fs, a.bucket, f'{root}/index-v2.json')
                if not canonical:
                    raise ValueError('canonical image index is missing')
                indexes = [read_json(fs, a.bucket, f'{root}/index-v2-{shard:02d}.json')
                           for shard in range(a.shard_count)]
                baselines = [shard_baseline(index or {'files': []}, canonical, number, a.shard_count)
                             for number, index in enumerate(indexes)]
                combined = {'files': [entry for index in baselines for entry in index['files']],
                            'failures': [entry for index in baselines for entry in index.get('failures', [])]}
            else:
                canonical = read_json(fs, a.bucket, f'{root}/index.json')
                indexes = [read_json(fs, a.bucket, f'{root}/index-{shard:02d}.json')
                           for shard in range(a.shard_count)]
                source_extensions = {extension}
                if a.kind == "media":
                    source_extensions = {str(x["key"].rsplit(".", 1)[-1]).lower() for x in candidates}
                combined = merge_shards(canonical, [(number, index) for number, index in enumerate(indexes) if index],
                                        "stream-index", source_extensions, a.shard_count)
            if a.verify_source_sha:
                # Reuse must obey the same profile gate as completion: a bundle
                # from another profile is stale even when its bytes match.
                # An explicit or future retry must not be suppressed by a
                # same-SHA success from before the recorded failure.
                failed_keys = {entry['key'] for entry in combined.get('failures', [])}
                reuse = {**combined, "files": [entry for entry in combined.get("files", [])
                         if entry['key'] not in failed_keys
                         and (expected is None or entry.get("profile") == expected)]}
                unchanged = unchanged_sources(candidates, reuse, token)
            else:
                unchanged = set()
            pending_count = 0
            for shard in range(a.shard_count):
                if shard_index is not None and shard != shard_index:
                    continue
                index = baselines[shard] if a.kind == 'image' else indexes[shard]
                completed = completed_entries(index, a.retry_failures, expected)
                if a.kind != 'image':
                    completed.update(completed_entries(combined, a.retry_failures, expected))
                pending = [x for x in candidates if completed.get(x["key"]) != x["revision"] and
                            x["key"] not in unchanged and
                            shard_for(x["key"], a.shard_count) == shard]
                pending_count += len(pending)
                if pending:
                    include.append({"extension": extension, "shard": shard})
            summaries.append({"extension": extension, "sources": len(candidates), "pending": pending_count,
                              "unchanged_sha": len(unchanged), "checkpoint_failures": len(combined.get("failures", []))})
        else:
            index = read_json(fs, a.bucket, f"{root_for(a.kind, extension)}/index.json")
            if a.kind == 'static-pdf':
                try:
                    from .pilot_static_pdf_stream import pending_sources as static_pending
                except ImportError:
                    from pilot_static_pdf_stream import pending_sources as static_pending
                pending, unchanged = static_pending(candidates, index, extension, token,
                                                     retry_failures=a.retry_failures)
                pending_count = len(pending)
            else:
                completed = completed_entries(index, a.retry_failures, expected)
                pending_count = sum(completed.get(x['key']) != x['revision'] for x in candidates)
            if pending_count:
                include.append({"extension": extension})
            summaries.append({"extension": extension, "sources": len(candidates),
                              "pending": pending_count, "checkpoint_failures": len(index.get("failures", [])),
                              **({'unchanged_sha': len(unchanged)} if a.kind == 'static-pdf' else {})})
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps({"include": include}, separators=(",", ":")) + "\n", encoding="utf-8")
    print(json.dumps({"include": include}, ensure_ascii=False))
    if a.report:
        a.report.parent.mkdir(parents=True, exist_ok=True)
        a.report.write_text(json.dumps({"kind": a.kind, "extensions": summaries}, ensure_ascii=False,
                                      indent=2) + "\n", encoding="utf-8")
        for summary in summaries:
            print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
