#!/usr/bin/env python3
"""Build incremental selectable HTML worksheet streams."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path

try:
    from .convert_reader_assets import convert_file, download_source
    from .reader_assets import SPREADSHEET_HTML_PROFILE, decode_search_payload, relative_path, source_url
    from .reader_stream_index import read_index as read_stream_index, run_documents
except ImportError:
    from convert_reader_assets import convert_file, download_source
    from reader_assets import SPREADSHEET_HTML_PROFILE, decode_search_payload, relative_path, source_url
    from reader_stream_index import read_index as read_stream_index, run_documents


BUCKET = "vomebook/reader-assets-v2"
EXTENSIONS = {"xls", "xlsx", "csv", "ods"}


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--search-data", type=Path, default=Path("output/search_data.json"))
    p.add_argument("--revisions", type=Path, default=Path("state/commits.json"))
    p.add_argument("--extension", default="all")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--bucket", default=BUCKET)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--retry-failures", action="store_true")
    p.add_argument("--strict", action="store_true")
    return p.parse_args()


def download(url, target, token):
    download_source(url, target)


def read_index(bucket, root, token):
    return read_stream_index(bucket, f"{root}/index.json", token, "spreadsheet-html-stream-index")


def select(path, revisions, extension):
    rows = decode_search_payload(json.loads(path.read_text(encoding="utf-8"))); result = []
    for row in rows:
        ext = str(row.get("Extension") or "").lower().lstrip(".")
        if ext not in EXTENSIONS or (extension != "all" and ext != extension): continue
        repo = str(row.get("Repo") or "")
        if repo and revisions.get(repo): result.append({"repo": repo, "path": relative_path(row), "extension": ext, "revision": revisions[repo]})
    return sorted(result, key=lambda x: (x["extension"], x["repo"], x["path"]))


def build(item, work, token, bucket):
    work.mkdir(parents=True, exist_ok=True); source = work / f"source.{item['extension']}"
    download(source_url(item["repo"], item["revision"], item["path"]), source, token)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest(); output = work / "document.html"
    # Reuse the shared encryption/mislabeled-XLS(X) handling. CSV and ODS must
    # never be passed to msoffcrypto's Office-only container parser.
    convert_file({**item, "profile": SPREADSHEET_HTML_PROFILE, "reader_mode": "html"}, source, output, work)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    bundle_digest = hashlib.sha256(f"{source_hash}\0{digest}".encode()).hexdigest()
    root = f"documents/spreadsheet/{item['extension']}/{bundle_digest}"
    obj, manifest_path = f"{root}/document.html", f"{root}/worksheet-manifest.json"
    manifest = work / "worksheet-manifest.json"
    manifest.write_text(json.dumps({"kind": "spreadsheet-html-stream", "version": 1, "source_extension": item["extension"], "source_sha256": source_hash, "path": "document.html", "bytes": output.stat().st_size, "sha256": digest}, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return {"key": f"{item['repo']}\0{item['path']}", "repo": item["repo"], "path": item["path"], "extension": item["extension"], "source_revision": item["revision"], "source_sha256": source_hash, "mode": "document-stream", "bucket": bucket, "object": obj, "manifest": manifest_path, "bytes": output.stat().st_size, "sha256": digest}, {obj: str(output), manifest_path: str(manifest)}


def main():
    a = parse_args()
    if a.limit < 0: raise ValueError("limit must be non-negative")
    ext = a.extension.lower().lstrip(".")
    if ext != "all" and ext not in EXTENSIONS: raise ValueError(f"unsupported spreadsheet extension: {ext}")
    token = os.environ.get("HF_TOKEN"); revisions = json.loads(a.revisions.read_text(encoding="utf-8")); selected = select(a.search_data, revisions, ext)
    with tempfile.TemporaryDirectory(prefix="reader-spreadsheet-") as directory:
        return run_documents(selected, bucket=a.bucket, token=token, limit=a.limit,
                             retry_failures=a.retry_failures, strict=a.strict, apply=a.apply,
                             prefix="documents/spreadsheet", kind="spreadsheet-html-stream-index",
                             build=build, work=Path(directory))


if __name__ == "__main__": raise SystemExit(main())
