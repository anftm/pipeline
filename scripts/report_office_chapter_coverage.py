#!/usr/bin/env python3
"""Report exact office chapter coverage against one pinned metadata snapshot."""

from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import hashlib
import io
import json
import os
import re
from collections import Counter
from pathlib import Path

from huggingface_hub import HfFileSystem

try:
    from .pilot_epub_chapters import (OFFICE_EXTENSIONS, OFFICE_MAX_SEARCH_INDEX_EXPANDED_BYTES,
                                      OFFICE_PROFILE, OFFICE_ROOT)
    from .reader_assets import decode_search_payload, relative_path, validate_chapter_manifest
    from .reader_bucket import read_bytes as read_bucket_bytes
except ImportError:
    from pilot_epub_chapters import (OFFICE_EXTENSIONS, OFFICE_MAX_SEARCH_INDEX_EXPANDED_BYTES,
                                     OFFICE_PROFILE, OFFICE_ROOT)
    from reader_assets import decode_search_payload, relative_path, validate_chapter_manifest
    from reader_bucket import read_bytes as read_bucket_bytes


TARGET_BUCKET = "vomebook/reader-assets-v2"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--search-data", type=Path, default=Path("output/search_data.json"))
    parser.add_argument("--revisions", type=Path, default=Path("state/commits.json"))
    parser.add_argument("--bucket", default=TARGET_BUCKET)
    parser.add_argument("--index", default=f"{OFFICE_ROOT}/index.json")
    parser.add_argument("--verify-bundles", action="store_true",
                        help="download and verify every current manifest and complete search index")
    parser.add_argument("--verify-workers", type=int, default=4)
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
        if not repo or not path:
            raise ValueError(f"office source lacks a stable repository/path identity: {record!r}")
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
            and entry.get("bucket") == TARGET_BUCKET
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
        if not source["source_revision"]:
            status = "missing-revision"
        elif current_entry and _usable_mapping(current_entry, source):
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
                  ("mapped", "pending", "failed", "stale-mapping", "invalid-mapping", "missing-revision")}
        values["total"] = sum(values.values())
        by_extension[extension] = values
    totals = {status: sum(values[status] for values in by_extension.values())
              for status in ("mapped", "pending", "failed", "stale-mapping", "invalid-mapping", "missing-revision")}
    totals["total"] = sum(totals.values())
    return {"version": 1, "kind": "office-chapter-coverage", "profile": OFFICE_PROFILE,
            "complete": totals["total"] > 0 and totals["mapped"] == totals["total"],
            "totals": totals, "by_extension": by_extension, "sources": rows}


def verify_bundle_payload(entry: dict, manifest_raw: bytes, search_raw: bytes,
                          first_chapter_raw: bytes) -> dict:
    manifest = validate_chapter_manifest(json.loads(manifest_raw.decode("utf-8")))
    if (manifest.get("kind") != "epub-chapters"
            or len(manifest["chapters"]) != entry.get("chapter_count")):
        raise ValueError("chapter manifest count/kind disagrees with canonical index")
    search_ref = manifest.get("search_index")
    if not isinstance(search_ref, dict):
        raise ValueError("chapter manifest has no complete search index")
    if (len(search_raw) != search_ref.get("bytes")
            or hashlib.sha256(search_raw).hexdigest() != search_ref.get("sha256")):
        raise ValueError("search-index size or checksum mismatch")
    with gzip.GzipFile(fileobj=io.BytesIO(search_raw), mode="rb") as stream:
        expanded = stream.read(OFFICE_MAX_SEARCH_INDEX_EXPANDED_BYTES + 1)
    if len(expanded) > OFFICE_MAX_SEARCH_INDEX_EXPANDED_BYTES:
        raise ValueError("search index exceeds the browser Worker expanded-text limit")
    search = json.loads(expanded.decode("utf-8"))
    if not isinstance(search, dict):
        raise ValueError("search index is not a JSON object")
    search_chapters = search.get("chapters")
    if (search.get("version") != 1 or search.get("kind") != "epub-search-index"
            or not isinstance(search_chapters, list)
            or len(search_chapters) != len(manifest["chapters"])):
        raise ValueError("search index does not contain the complete chapter set")
    for chapter, indexed in zip(manifest["chapters"], search_chapters):
        if (indexed.get("index") != chapter["index"] or indexed.get("path") != chapter["path"]
                or indexed.get("title") != chapter["title"] or not isinstance(indexed.get("text"), str)):
            raise ValueError("search index chapter identity differs from the manifest")
    first_chapter = manifest["chapters"][0]
    if (len(first_chapter_raw) != first_chapter["bytes"]
            or hashlib.sha256(first_chapter_raw).hexdigest() != first_chapter["sha256"]):
        raise ValueError("first chapter size or checksum mismatch")
    return {"chapter_count": len(manifest["chapters"]),
            "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
            "search_index_sha256": hashlib.sha256(search_raw).hexdigest(),
            "first_chapter_sha256": hashlib.sha256(first_chapter_raw).hexdigest()}


def verify_processing_payload(record: dict, entry: dict, bucket: str) -> str:
    generation = record.get("generation")
    if (record.get("kind") != "reader-processing-roots" or record.get("status") != "released"
            or not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{32}", generation)):
        raise ValueError("office processing root was not released by a catalog generation")
    if not any(resource.get("bucket") == bucket and resource.get("root") == entry["root"]
               for resource in record.get("resources", []) if isinstance(resource, dict)):
        raise ValueError("released office processing root does not match the chapter bundle")
    return generation


def verify_remote_bundle(entry: dict, bucket: str, token: str) -> dict:
    manifest_raw = read_bucket_bytes(entry["manifest"], token, bucket)
    manifest = json.loads(manifest_raw.decode("utf-8"))
    search_path = f"{entry['root']}/{manifest['search_index']['path']}"
    search_raw = read_bucket_bytes(search_path, token, bucket)
    first_chapter_raw = read_bucket_bytes(
        f"{entry['root']}/{manifest['chapters'][0]['path']}", token, bucket)
    result = verify_bundle_payload(entry, manifest_raw, search_raw, first_chapter_raw)
    roots = entry.get("processing_roots") or []
    if not roots:
        raise ValueError("office chapter mapping has no processing-root receipt")
    for path in roots:
        record = json.loads(read_bucket_bytes(path, token, bucket).decode("utf-8"))
        verify_processing_payload(record, entry, bucket)
    return result


def verify_bundles(report: dict, index: dict, bucket: str, token: str, workers: int) -> dict:
    if type(workers) is not int or not 1 <= workers <= 16:
        raise ValueError("verify-workers must be 1..16")
    by_key = {item.get("key"): item for item in index.get("files", [])
              if isinstance(item, dict) and isinstance(item.get("key"), str)}
    mapped = [row for row in report["sources"] if row["status"] == "mapped"]
    errors = []
    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(verify_remote_bundle, by_key[row["key"]], bucket, token): row
                   for row in mapped}
        for future in concurrent.futures.as_completed(futures):
            row = futures[future]
            try:
                future.result()
            except Exception as error:
                errors.append({"key": row["key"], "error": f"{type(error).__name__}: {error}"})
            completed += 1
            if completed % 100 == 0:
                print(f"verified office bundles: {completed}/{len(mapped)}", flush=True)
    return {"enabled": True, "checked": completed, "valid": completed - len(errors),
            "failed": len(errors), "errors": sorted(errors, key=lambda item: item["key"])}


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
    if args.verify_bundles:
        report["bundle_verification"] = verify_bundles(report, index, args.bucket, token, args.verify_workers)
        report["complete"] = report["complete"] and report["bundle_verification"]["failed"] == 0
    else:
        report["bundle_verification"] = {"enabled": False}
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
