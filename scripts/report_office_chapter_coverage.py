#!/usr/bin/env python3
"""Report exact office chapter coverage against one pinned metadata snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from pathlib import Path

from huggingface_hub import HfFileSystem

try:
    from .pilot_epub_chapters import OFFICE_EXTENSIONS, OFFICE_PROFILE, OFFICE_ROOT
    from .reader_assets import decode_search_payload, relative_path
except ImportError:
    from pilot_epub_chapters import OFFICE_EXTENSIONS, OFFICE_PROFILE, OFFICE_ROOT
    from reader_assets import decode_search_payload, relative_path


TARGET_BUCKET = "vomebook/reader-assets-v2"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--search-data", type=Path, default=Path("output/search_data.json"))
    parser.add_argument("--revisions", type=Path, default=Path("state/commits.json"))
    parser.add_argument("--bucket", default=TARGET_BUCKET)
    parser.add_argument("--index", default=f"{OFFICE_ROOT}/index.json")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def read_index(fs, bucket: str, path: str) -> dict:
    try:
        with fs.open(f"hf://buckets/{bucket}/{path}", "rb") as stream:
            value = json.loads(stream.read().decode("utf-8"))
    except FileNotFoundError:
        return {"version": 1, "kind": "ebook-chapter-stream-index", "files": [], "failures": []}
    if (not isinstance(value, dict) or not isinstance(value.get("files"), list)
            or not isinstance(value.get("failures", []), list)):
        raise ValueError(f"invalid office chapter index: {bucket}/{path}")
    return value


def office_sources(search_data: dict, revisions: dict) -> list[dict]:
    sources = {}
    for record in decode_search_payload(search_data):
        extension = str(record.get("Extension") or "").lower().lstrip(".")
        if extension not in OFFICE_EXTENSIONS:
            continue
        repo = str(record.get("Repo") or "")
        path = relative_path(record)
        revision = str(revisions.get(repo) or "")
        if not repo or not path or not revision:
            continue
        key = f"{repo}\0{path}"
        candidate = {"key": key, "repo": repo, "path": path,
                     "extension": extension, "source_revision": revision}
        previous = sources.get(key)
        if previous and previous != candidate:
            raise ValueError(f"conflicting current metadata for office source key {key!r}")
        sources[key] = candidate
    return [sources[key] for key in sorted(sources)]


def _usable_mapping(entry: dict, source: dict) -> bool:
    return (entry.get("profile") == OFFICE_PROFILE
            and entry.get("source_extension") == source["extension"]
            and entry.get("extension") == "epub"
            and isinstance(entry.get("root"), str)
            and entry["root"].startswith(f"{OFFICE_ROOT}/")
            and isinstance(entry.get("manifest"), str)
            and entry["manifest"] == f"{entry['root']}/chapter-manifest.json"
            and isinstance(entry.get("search_index"), str)
            and entry["search_index"] == f"{entry['root']}/epub-search-index.json.gz"
            and type(entry.get("chapter_count")) is int and entry["chapter_count"] > 0
            and isinstance(entry.get("source_sha256"), str)
            and len(entry["source_sha256"]) == 64)


def summarize(sources: list[dict], index: dict) -> dict:
    files = {item["key"]: item for item in index.get("files", [])
             if isinstance(item, dict) and isinstance(item.get("key"), str)}
    failures = {item["key"]: item for item in index.get("failures", [])
                if isinstance(item, dict) and isinstance(item.get("key"), str)}
    rows, states = [], Counter()
    for source in sources:
        entry = files.get(source["key"])
        failure = failures.get(source["key"])
        current_entry = (entry if isinstance(entry, dict)
                         and entry.get("source_revision") == source["source_revision"] else None)
        current_failure = (failure if isinstance(failure, dict)
                           and failure.get("source_revision") == source["source_revision"] else None)
        if current_entry and _usable_mapping(current_entry, source):
            status = "mapped"
        elif current_entry:
            status = "invalid-mapping"
        elif current_failure:
            status = "failed"
        elif entry:
            status = "stale-mapping"
        else:
            status = "pending"
        states[(source["extension"], status)] += 1
        rows.append({**source, "status": status,
                     **({"manifest": current_entry.get("manifest"),
                        "chapter_count": current_entry.get("chapter_count"),
                        "source_sha256": current_entry.get("source_sha256")}
                       if current_entry else {}),
                     **({"error": current_failure.get("error", "")}
                       if current_failure else {})})
    by_extension = {}
    for extension in sorted(OFFICE_EXTENSIONS):
        values = {status: states[(extension, status)] for status in
                  ("mapped", "pending", "failed", "stale-mapping", "invalid-mapping")}
        values["total"] = sum(values.values())
        by_extension[extension] = values
    totals = {status: sum(values[status] for values in by_extension.values())
              for status in ("mapped", "pending", "failed", "stale-mapping", "invalid-mapping")}
    totals["total"] = sum(totals.values())
    return {"version": 1, "kind": "office-chapter-coverage", "profile": OFFICE_PROFILE,
            "complete": totals["total"] > 0 and totals["mapped"] == totals["total"],
            "totals": totals, "by_extension": by_extension, "sources": rows}


def main() -> int:
    args = parse_args()
    raw_search = args.search_data.read_bytes()
    raw_revisions = args.revisions.read_bytes()
    search_data = json.loads(raw_search)
    revisions = json.loads(raw_revisions)
    if not isinstance(revisions, dict):
        raise ValueError("repository revisions must be a JSON object")
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise RuntimeError("HF_TOKEN is required")
    fs = HfFileSystem(token=token)
    index = read_index(fs, args.bucket, args.index)
    report = summarize(office_sources(search_data, revisions), index)
    report["snapshot"] = {
        "search_data_sha256": hashlib.sha256(raw_search).hexdigest(),
        "revisions_sha256": hashlib.sha256(raw_revisions).hexdigest(),
        "index": f"{args.bucket}/{args.index}",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                           encoding="utf-8")
    print(json.dumps({"complete": report["complete"], "totals": report["totals"],
                      "by_extension": report["by_extension"], "output": str(args.output)},
                     ensure_ascii=False, sort_keys=True))
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
