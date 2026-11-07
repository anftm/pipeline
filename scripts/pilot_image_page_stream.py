#!/usr/bin/env python3
"""Build and publish image assets for ordinary image sources.

Single-frame sources become one WebP (``mode: image``). Multi-page TIFFs keep
their pages as an ``image-page-stream`` page manifest instead of being wrapped
in a PDF. Animated GIFs are reduced to their first frame and published as one
static WebP. The canonical index is ``pages/image/index-v2.json``. Uploads
are content-addressed and protected before checkpointing; the old image index
and its objects remain untouched.
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

from huggingface_hub.errors import HfHubHTTPError
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = 300_000_000

try:
    from .reader_assets import decode_search_payload, relative_path, source_url
    from .reader_stream_index import (read_index, validate_index, unchanged_sources, publish_checkpoint,
                                      shard_for, source_failure)
except ImportError:
    from reader_assets import decode_search_payload, relative_path, source_url
    from reader_stream_index import (read_index, validate_index, unchanged_sources, publish_checkpoint,
                                     shard_for, source_failure)


TARGET_BUCKET = "vomebook/reader-assets-v2"
IMAGE_ROOT = "pages/image"
INDEX_PREFIX = "index-v2"
INDEX_NAME = "index-v2.json"
SUPPORTED_EXTENSIONS = {"jpg", "jpeg", "png", "bmp", "webp", "gif", "tif", "tiff"}
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
    parser.add_argument("--retry-failures", action="store_true",
                        help="retry recorded conversion failures")
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


def build_one(item: dict, work: Path, token: str | None, bucket: str, *,
              legacy_source_addressed: bool = False) -> tuple[dict, dict[str, str]]:
    work.mkdir(parents=True, exist_ok=True)
    source = work / "source"
    download(source_url(item["repo"], item["revision"], item["path"]), source, token)
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if item.get("source_sha256") and item["source_sha256"] != source_digest:
        raise ValueError("image source checksum differs from pinned repair identity")
    if item.get("bytes") and source.stat().st_size != item["bytes"]:
        raise ValueError("image source byte count differs from pinned metadata")
    extension = item["extension"]
    with Image.open(source) as image:
        if image.width < 1 or image.height < 1:
            raise ValueError("image has invalid dimensions")
        if extension in {"tif", "tiff"} and int(getattr(image, "n_frames", 1) or 1) > 1:
            return build_pages(item, source, image, work, extension, source_digest, bucket,
                               legacy_source_addressed=legacy_source_addressed)
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
        converted.save(work / "document.webp", "WEBP", quality=85, method=6)
        converted.close()
    output = work / "document.webp"
    data = output.read_bytes()
    root = (f'{IMAGE_ROOT}/{extension}/{source_digest}' if legacy_source_addressed else
            image_root(extension, source_digest, {'document.webp': hashlib.sha256(data).hexdigest()}))
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
        "root": root,
        "path": remote,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "page_count": 1,
    }, {remote: str(output)}


def image_root(extension, source_digest, files):
    """Keep the existing Reader URL shape while binding it to output bytes."""
    identity = json.dumps({'version': 1, 'source_sha256': source_digest, 'files': files},
                          sort_keys=True, separators=(',', ':')).encode()
    return f"{IMAGE_ROOT}/{extension}/{hashlib.sha256(identity).hexdigest()}"


def build_pages(item, source, image, work, extension, source_digest, bucket, *, legacy_source_addressed=False):
    """Publish every TIFF frame as an image page stream, never a PDF wrapper."""
    pages = work / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    entries, local_pages = [], {}
    for number in range(1, int(image.n_frames) + 1):
        image.seek(number - 1)
        converted = image.convert("RGB")
        if max(converted.size) > MAX_WEBP_EDGE:
            scale = MAX_WEBP_EDGE / max(converted.size)
            resized = converted.resize((max(1, round(converted.width * scale)),
                                        max(1, round(converted.height * scale))),
                                       Image.Resampling.LANCZOS)
            converted.close()
            converted = resized
        target = pages / f"page-{number:06d}.webp"
        converted.save(target, "WEBP", quality=85, method=6)
        converted.close()
        payload = target.read_bytes()
        relative = f"pages/page-{number:06d}.webp"
        entries.append({"path": relative, "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest()})
        local_pages[relative] = str(target)
    manifest = {"kind": "image-page-stream", "version": 1, "source_extension": extension,
                "source_sha256": source_digest, "page_count": len(entries), "pages": entries}
    manifest_file = work / "page-manifest.json"
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                             encoding="utf-8")
    identities = {entry['path']: entry['sha256'] for entry in entries}
    identities['page-manifest.json'] = hashlib.sha256(manifest_file.read_bytes()).hexdigest()
    root = (f'{IMAGE_ROOT}/{extension}/{source_digest}' if legacy_source_addressed else
            image_root(extension, source_digest, identities))
    remote = f"{root}/page-manifest.json"
    uploads = {f'{root}/{path}': local for path, local in local_pages.items()}
    uploads[remote] = str(manifest_file)
    return {
        "key": f"{item['repo']}\0{item['path']}", "repo": item["repo"],
        "source_path": item["path"], "extension": extension,
        "source_revision": item["revision"], "source_bytes": source.stat().st_size,
        "source_sha256": source_digest, "mode": "page-stream", "bucket": bucket,
        "root": root, "manifest": remote, "page_count": len(entries),
        "bytes": sum(entry["bytes"] for entry in entries),
    }, uploads


def existing_index(bucket: str, token: str | None, name: str) -> dict:
    payload = read_index(bucket, f'{IMAGE_ROOT}/{name}', token, 'image-stream-index')
    if name == INDEX_NAME and not payload['files']:
        raise ValueError('canonical image index is missing or empty')
    return payload


def shard_baseline(shard, canonical, number, count):
    """Keep canonical routes; never let an unversioned stale shard replace one."""
    validate_index(shard, 'image shard')
    validate_index(canonical, 'canonical image index')
    if len({entry['key'] for entry in canonical['files']}) != len(canonical['files']):
        raise ValueError('duplicate canonical image key')
    if count == 1:
        return canonical
    current = {e['key']: e for e in canonical['files'] if shard_for(e['key'], count) == number}
    seen = set()
    for entry in shard['files']:
        key = entry['key']
        if key in seen or shard_for(key, count) != number:
            raise ValueError('image shard contains a duplicate or foreign key')
        seen.add(key)
        old = current.get(key)
        if old is not None and old != entry:
            a, b = old.get('checkpointed_at', ''), entry.get('checkpointed_at', '')
            if not b:
                if not a:
                    raise ValueError('ambiguous image shard and canonical mapping')
                continue
            if a and a == b:
                raise ValueError('ambiguous image shard and canonical mapping')
            if a and b < a:
                continue
        current[key] = entry
    failures = [e for e in shard.get('failures', []) if shard_for(e['key'], count) == number]
    if len(failures) != len(shard.get('failures', [])):
        raise ValueError('image shard contains a foreign failure')
    return {'version': 1, 'kind': 'image-stream-index',
            'files': [current[k] for k in sorted(current)], 'failures': failures}


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
    shard = existing_index(args.bucket, token, index_name)
    canonical = shard if args.shard_count == 1 else existing_index(args.bucket, token, INDEX_NAME)
    previous = shard_baseline(shard, canonical, args.shard_index, args.shard_count)
    retry_failures = getattr(args, 'retry_failures', False)
    completed = {entry['key']: entry.get('source_revision') for entry in previous['files']}
    if not retry_failures:
        completed.update({entry['key']: entry.get('source_revision') for entry in previous.get('failures', [])})
    else:
        # A failed retry may coexist with a previous success at the same source
        # revision. Explicit retry must override that success, in both planner
        # and worker, rather than silently turning into a no-op.
        for failure in previous.get('failures', []):
            completed.pop(failure['key'], None)
    selected = [item for item in selected
                if int.from_bytes(hashlib.sha256(
                    f"{item['repo']}\0{item['path']}".encode()).digest()[:8], "big") % args.shard_count == args.shard_index
                if completed.get(f"{item['repo']}\0{item['path']}") != item["revision"]]
    # Dataset commits often advance because unrelated files changed. Reuse an
    # existing image only when the source's pinned LFS SHA-256 still matches;
    # absent/unknown hashes continue through normal download and conversion.
    failed_keys = {entry['key'] for entry in previous.get('failures', [])}
    reuse = {**previous, 'files': [e for e in previous['files'] if e['key'] not in failed_keys]}
    unchanged = unchanged_sources(selected, reuse, token)
    selected = [item for item in selected if f"{item['repo']}\0{item['path']}" not in unchanged]
    print(f"unchanged_by_source_sha={len(unchanged)} pending={len(selected)}", flush=True)
    if args.limit:
        selected = selected[:args.limit]
    if not selected:
        print("no matching image sources")
        return 0
    converted = failed = 0
    with tempfile.TemporaryDirectory(prefix="reader-image-v2-") as directory:
        work_root = Path(directory)
        for number, item in enumerate(selected):
            folder = work_root / str(number)
            folder.mkdir()
            try:
                result, files = build_one(item, folder / 'bundle', token, args.bucket)
                entries, failures = [result], []
                converted += 1
            except Exception as error:
                entries, failures, files = [], [source_failure(item, error)], {}
                failed += 1
                print(f"failed: {item['repo']}/{item['path']}: {type(error).__name__}: {error}")
            previous = publish_checkpoint(args.bucket, f'{IMAGE_ROOT}/{index_name}', previous,
                                          entries, failures, files, folder, token,
                                          apply=args.apply, kind='image-stream-index')
        print(f'attempted={len(selected)} converted={converted} failures={failed} apply={args.apply}')
    return 1 if args.strict and failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
