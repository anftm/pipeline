#!/usr/bin/env python3
"""Build and publish single-image WebP assets for ordinary image sources.

TIF/TIFF stay on the PDF conversion path. Animated GIFs are reduced to their
first frame and published as one static WebP. The canonical index is
``pages/image/index-v2.json`` so a full rebuild never merges old page-stream
entries; old ``pages/image/index.json`` remains untouched until the v2 index is
complete and the Reader sidecar is switched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
import urllib.request
from pathlib import Path

from huggingface_hub import HfFileSystem
from huggingface_hub.errors import HfHubHTTPError
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = 300_000_000

try:
    from .reader_assets import decode_search_payload, relative_path, source_url
    from .shared import batch_bucket_files_with_retry
except ImportError:
    from reader_assets import decode_search_payload, relative_path, source_url
    from shared import batch_bucket_files_with_retry


TARGET_BUCKET = "vomebook/reader-assets-v2"
IMAGE_ROOT = "pages/image"
INDEX_PREFIX = "index-v2"
INDEX_NAME = "index-v2.json"
SUPPORTED_EXTENSIONS = {"jpg", "jpeg", "png", "bmp", "webp", "gif"}
MAX_WEBP_EDGE = 8_192
DOWNLOAD_INTERVAL_SECONDS = 0.35
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
DOWNLOAD_ATTEMPTS = 5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--search-data", type=Path, default=Path("output/search_data.json"))
    parser.add_argument("--revisions", type=Path, default=Path("state/commits.json"))
    parser.add_argument("--limit", type=int, default=0,
                        help="maximum sources; 0 means all")
    parser.add_argument("--max-source-bytes", type=int, default=0,
                        help="maximum source bytes; 0 means unlimited")
    parser.add_argument("--extension", default="all")
    parser.add_argument("--bucket", default=TARGET_BUCKET)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rebuild", action="store_true",
                        help="ignore the existing v2 shard index (full rebuild)")
    parser.add_argument("--strict", action="store_true",
                        help="fail the shard if any selected image fails")
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    return parser.parse_args()


def retry_delay(error: HfHubHTTPError, attempt: int) -> float:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None) or {}
    retry_after = headers.get("Retry-After")
    if retry_after:
        try:
            return max(5.0, float(retry_after))
        except (TypeError, ValueError):
            pass
    reset = headers.get("X-RateLimit-Reset")
    if reset:
        try:
            return max(5.0, float(reset) - time.time() + 5.0)
        except (TypeError, ValueError):
            pass
    return min(300.0, 30.0 * (2 ** attempt))


def download(url: str, target: Path, token: str | None) -> None:
    from huggingface_hub.utils import get_session, hf_raise_for_status
    for attempt in range(DOWNLOAD_ATTEMPTS):
        try:
            with get_session().stream("GET", url,
                                      headers={"Authorization": f"Bearer {token}"} if token else {},
                                      follow_redirects=True, timeout=120) as response:
                hf_raise_for_status(response)
                with target.open("wb") as stream:
                    for chunk in response.iter_bytes():
                        stream.write(chunk)
            time.sleep(DOWNLOAD_INTERVAL_SECONDS)
            return
        except HfHubHTTPError as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            if status not in RETRYABLE_STATUS or attempt == DOWNLOAD_ATTEMPTS - 1:
                raise
            delay = retry_delay(error, attempt)
            print(f"source download HTTP {status}; retrying in {delay:.0f}s "
                  f"({attempt + 1}/{DOWNLOAD_ATTEMPTS})")
            time.sleep(delay)


def source_records(path: Path, revisions: dict, max_bytes: int, extension_filter: str) -> list[dict]:
    records = decode_search_payload(json.loads(path.read_text(encoding="utf-8")))
    selected = []
    for record in records:
        extension = str(record.get("Extension") or "").lower().lstrip(".")
        size = int(record.get("Size") or 0)
        if (extension not in SUPPORTED_EXTENSIONS
                or (extension_filter != "all" and extension != extension_filter)
                or size <= 0 or (max_bytes and size > max_bytes)):
            continue
        repo = str(record.get("Repo") or "")
        revision = str(revisions.get(repo) or "")
        if not repo or not revision:
            continue
        selected.append({
            "repo": repo,
            "path": relative_path(record),
            "extension": extension,
            "bytes": size,
            "revision": revision,
        })
    selected.sort(key=lambda item: (item["bytes"], item["repo"], item["path"]))
    return selected


def build_one(item: dict, work: Path, token: str | None, bucket: str) -> tuple[dict, dict[str, str]]:
    work.mkdir(parents=True, exist_ok=True)
    source = work / "source"
    download(source_url(item["repo"], item["revision"], item["path"]), source, token)
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if item.get("source_sha256") and item["source_sha256"] != source_digest:
        raise ValueError("image source checksum differs from pinned repair identity")
    if item.get("bytes") and source.stat().st_size != item["bytes"]:
        raise ValueError("image source byte count differs from pinned metadata")
    extension = item["extension"]
    root = f"{IMAGE_ROOT}/{extension}/{source_digest}"
    output = work / "document.webp"
    with Image.open(source) as image:
        if image.width < 1 or image.height < 1:
            raise ValueError("image has invalid dimensions")
        # GIF (and any other animated image) is represented by its first frame.
        try:
            image.seek(0)
        except EOFError:
            pass
        converted = image.convert("RGB")
        if max(converted.size) > MAX_WEBP_EDGE:
            scale = MAX_WEBP_EDGE / max(converted.size)
            resized = converted.resize(
                (max(1, round(converted.width * scale)),
                 max(1, round(converted.height * scale))),
                Image.Resampling.LANCZOS,
            )
            converted.close()
            converted = resized
        converted.save(output, "WEBP", quality=85, method=6)
        converted.close()
    data = output.read_bytes()
    remote = f"{root}/document.webp"
    return {
        "key": f"{item['repo']}\0{item['path']}",
        "repo": item["repo"],
        "source_path": item["path"],
        "extension": extension,
        "source_revision": item["revision"],
        "source_bytes": source.stat().st_size,
        "source_sha256": source_digest,
        "mode": "image",
        "bucket": bucket,
        "path": remote,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "page_count": 1,
    }, {remote: str(output)}


def existing_index(bucket: str, token: str | None, name: str) -> dict:
    path = f"hf://buckets/{bucket}/{IMAGE_ROOT}/{name}"
    try:
        fs = HfFileSystem(token=token)
        with fs.open(path, "rb") as stream:
            payload = json.loads(stream.read().decode("utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("files"), list):
            return payload
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        pass
    return {"version": 1, "kind": "image-stream-index", "files": []}


def main() -> int:
    args = parse_args()
    if args.limit < 0 or args.max_source_bytes < 0:
        raise ValueError("limit and max-source-bytes must be non-negative")
    if args.shard_count < 1 or not 0 <= args.shard_index < args.shard_count:
        raise ValueError("invalid shard")
    token = os.environ.get("HF_TOKEN")
    revisions = json.loads(args.revisions.read_text(encoding="utf-8"))
    extension = args.extension.lower().lstrip(".")
    if extension != "all" and extension not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"unsupported image extension: {extension}")
    selected = source_records(args.search_data, revisions, args.max_source_bytes, extension)
    index_name = INDEX_NAME if args.shard_count == 1 else f"{INDEX_PREFIX}-{args.shard_index:02d}.json"
    previous = ({"version": 1, "kind": "image-stream-index", "files": []}
                if args.rebuild else existing_index(args.bucket, token, index_name))
    completed = {
        entry.get("key"): entry.get("source_revision")
        for entry in previous.get("files", []) if isinstance(entry, dict)
    }
    selected = [item for item in selected
                if int.from_bytes(hashlib.sha256(
                    f"{item['repo']}\0{item['path']}".encode()).digest()[:8], "big") % args.shard_count == args.shard_index
                if completed.get(f"{item['repo']}\0{item['path']}") != item["revision"]]
    if args.limit:
        selected = selected[:args.limit]
    if not selected:
        print("no matching image sources")
        return 0
    uploads: dict[str, str] = {}
    entries = []
    failures = 0
    with tempfile.TemporaryDirectory(prefix="reader-image-v2-") as directory:
        work_root = Path(directory)
        for index, item in enumerate(selected):
            try:
                result, files = build_one(item, work_root / str(index), token, args.bucket)
            except Exception as error:
                failures += 1
                print(f"failed: {item['repo']}/{item['path']}: {type(error).__name__}: {error}")
                continue
            entries.append(result)
            uploads.update(files)
        if not entries:
            print("no image source converted successfully")
            return 1
        previous_files = {} if args.rebuild else {
            entry.get("key"): entry for entry in previous.get("files", []) if isinstance(entry, dict)}
        previous_files.update({entry["key"]: entry for entry in entries})
        index_payload = {"version": 1, "kind": "image-stream-index", "files": [
            previous_files[key] for key in sorted(previous_files)
        ]}
        index_file = work_root / index_name
        index_file.write_text(json.dumps(index_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        uploads[f"{IMAGE_ROOT}/{index_name}"] = str(index_file)
        print(f"planned {len(entries)} image asset(s), {len(uploads)} object(s), failures={failures}")
        if args.apply:
            batch_bucket_files_with_retry(args.bucket, [(local, remote) for remote, local in sorted(uploads.items())], token)
            print(f"published {len(uploads)} object(s) to {args.bucket}")
        else:
            print("report-only; pass --apply to publish")
        if args.strict and failures:
            print(f"strict image rebuild failed for {failures} source(s)")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
