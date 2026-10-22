#!/usr/bin/env python3
"""Build and publish sanitized HTML/MHTML Reader document streams."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path

try:
    from .convert_reader_assets import download_source, inline_html_resources, mhtml_to_html, validate_html_content
    from .reader_assets import decode_search_payload, relative_path, source_url
    from .reader_stream_index import read_index, run_documents
except ImportError:
    from convert_reader_assets import download_source, inline_html_resources, mhtml_to_html, validate_html_content
    from reader_assets import decode_search_payload, relative_path, source_url
    from reader_stream_index import read_index, run_documents


TARGET_BUCKET = "vomebook/reader-assets-v2"
SUPPORTED_EXTENSIONS = {"htm", "html", "mht", "mhtml"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--search-data", type=Path, default=Path("output/search_data.json"))
    parser.add_argument("--revisions", type=Path, default=Path("state/commits.json"))
    parser.add_argument("--extension", default="all")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--bucket", default=TARGET_BUCKET)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--retry-failures", action="store_true")
    parser.add_argument("--strict", action="store_true")
    return parser.parse_args()


def download(url: str, target: Path, token: str | None) -> None:
    download_source(url, target)


def existing_index(bucket: str, token: str | None, root: str) -> dict:
    return read_index(bucket, f"{root}/index.json", token, "web-document-stream-index")


def select_records(path: Path, revisions: dict, extension: str) -> list[dict]:
    records = decode_search_payload(json.loads(path.read_text(encoding="utf-8")))
    result = []
    for record in records:
        ext = str(record.get("Extension") or "").lower().lstrip(".")
        if ext not in SUPPORTED_EXTENSIONS or (extension != "all" and ext != extension):
            continue
        repo = str(record.get("Repo") or "")
        revision = str(revisions.get(repo) or "")
        if repo and revision:
            result.append({"repo": repo, "path": relative_path(record), "extension": ext, "revision": revision})
    result.sort(key=lambda item: (item["extension"], item["repo"], item["path"]))
    return result


def build_one(item: dict, work: Path, token: str | None, bucket: str) -> tuple[dict, dict[str, str]]:
    work.mkdir(parents=True, exist_ok=True)
    source = work / f"source.{item['extension']}"
    download(source_url(item["repo"], item["revision"], item["path"]), source, token)
    raw_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if item["extension"] in {"mht", "mhtml"}:
        html_path = work / "document.html"
        mhtml_to_html(source, html_path)
    else:
        html_path = inline_html_resources(source, source_url(item["repo"], item["revision"], item["path"]), work)
    validate_html_content(html_path)
    output = html_path.read_bytes()
    output_digest = hashlib.sha256(output).hexdigest()
    # HTML can depend on same-tree images/CSS that changed without changing its
    # source bytes. Bind immutable output URLs to the complete inlined result.
    bundle_digest = hashlib.sha256(f"{raw_digest}\0{output_digest}".encode()).hexdigest()
    root = f"documents/web/{item['extension']}/{bundle_digest}"
    object_path = f"{root}/document.html"
    manifest_path = f"{root}/document-manifest.json"
    manifest_file = work / "document-manifest.json"
    manifest_file.write_text(json.dumps({
        "kind": "web-document-stream", "version": 1, "source_extension": item["extension"],
        "source_sha256": raw_digest, "path": "document.html", "bytes": len(output),
        "sha256": output_digest,
    }, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return {
        "key": f"{item['repo']}\0{item['path']}", "repo": item["repo"], "path": item["path"],
        "extension": item["extension"], "source_revision": item["revision"], "source_sha256": raw_digest,
        "mode": "document-stream", "bucket": bucket, "object": object_path, "manifest": manifest_path,
        "bytes": len(output), "sha256": output_digest,
    }, {object_path: str(html_path), manifest_path: str(manifest_file)}


def main() -> int:
    args = parse_args()
    if args.limit < 0:
        raise ValueError("limit must be non-negative")
    extension = args.extension.lower().lstrip(".")
    if extension != "all" and extension not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"unsupported web extension: {extension}")
    token = os.environ.get("HF_TOKEN")
    revisions = json.loads(args.revisions.read_text(encoding="utf-8"))
    selected_all = select_records(args.search_data, revisions, extension)
    with tempfile.TemporaryDirectory(prefix="reader-web-") as directory:
        return run_documents(selected_all, bucket=args.bucket, token=token, limit=args.limit,
                             retry_failures=args.retry_failures, strict=args.strict, apply=args.apply,
                             prefix="documents/web", kind="web-document-stream-index",
                             build=build_one, work=Path(directory))


if __name__ == "__main__":
    raise SystemExit(main())
