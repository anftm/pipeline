#!/usr/bin/env python3
"""Build and publish image assets for ordinary image sources.

Single-frame sources become one WebP (``mode: image``). Multi-page TIFFs keep
their pages as an ``image-page-stream`` page manifest instead of being wrapped
in a PDF. Animated GIFs are reduced to their first frame and published as one
static WebP. The canonical index is ``pages/image/index-v2.json``. Uploads
are content-addressed and protected before checkpointing; the old image index
and its objects remain untouched. Only the separate exact-source canary opts
in to AVIF.
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
    from . import reader_image_avif as avif
    from .reader_image_avif import image_root
    from .reader_assets import decode_search_payload, relative_path, source_url
    from .reader_stream_index import (read_index, validate_index, unchanged_sources, publish_checkpoint,
                                      shard_for, source_failure)
except ImportError:
    import reader_image_avif as avif
    from reader_image_avif import image_root
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
    parser.add_argument("--boost-canary", action="store_true")
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


def download(url: str, target: Path, token: str | None, *, exact_bytes: int | None = None) -> None:
    from huggingface_hub.utils import get_session, hf_raise_for_status
    if exact_bytes is not None and (type(exact_bytes) is not int or exact_bytes <= 0):
        raise ValueError("invalid pinned image source byte count")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if exact_bytes is not None:
        headers["Accept-Encoding"] = "identity"
    for attempt in range(DOWNLOAD_ATTEMPTS):
        try:
            with get_session().stream("GET", url, headers=headers,
                                      follow_redirects=True, timeout=120) as response:
                hf_raise_for_status(response)
                if exact_bytes is not None and (response.status_code != 200 or response.headers.get("Content-Range")
                                                or response.headers.get("Content-Encoding", "identity").lower() != "identity"):
                    raise ValueError("expected a complete unencoded pinned image response")
                remaining = exact_bytes
                with target.open("wb") as stream:
                    # Bound chunk size as well as total bytes written in canary mode.
                    chunks = (response.iter_bytes() if remaining is None else
                              response.iter_raw(chunk_size=min(64 * 1024, remaining + 1)))
                    for chunk in chunks:
                        if remaining is not None:
                            remaining -= len(chunk)
                            if remaining < 0:
                                raise ValueError("image download exceeds pinned byte count")
                        stream.write(chunk)
                if remaining not in (None, 0):
                    raise ValueError("image download is shorter than pinned byte count")
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


def encode_frame(image, target, *, codec, webp_mode='RGB'):
    mode = webp_mode
    if codec == 'avif':
        alpha = any(band.lower() == 'a' for band in image.getbands()) or 'transparency' in image.info
        mode = 'RGBA' if alpha else 'RGB'
    converted = image.convert(mode)
    try:
        if max(converted.size) > MAX_WEBP_EDGE:
            scale = MAX_WEBP_EDGE / max(converted.size)
            resized = converted.resize(tuple(max(1, round(n * scale)) for n in converted.size),
                                       Image.Resampling.LANCZOS)
            converted.close()
            converted = resized
        if codec == 'avif':
            import imagecodecs
            import numpy
            encoded = imagecodecs.avif_encode(numpy.asarray(converted), level=70, speed=6,
                                               pixelformat='yuv444', numthreads=1)
            if not 0 < len(encoded) <= avif.MAX_IMAGE_BYTES:
                raise ValueError('AVIF output exceeds the Reader image byte limit')
            target.write_bytes(encoded)
        else:
            converted.save(target, 'WEBP', quality=85, method=6)
    finally:
        converted.close()


def build_one(item: dict, work: Path, token: str | None, bucket: str, *,
              legacy_source_addressed: bool = False, boost_canary: bool = False,
              codec: str = 'webp', previous_source_sha256: str | None = None,
              max_tiff_pages: int = avif.MAX_PAGES) -> tuple[dict, dict[str, str]]:
    extension = item['extension']
    if codec != 'webp' and (codec != 'avif' or extension not in avif.FORMATS or legacy_source_addressed):
        raise ValueError('AVIF requires a supported non-GIF image and an immutable root')
    work.mkdir(parents=True, exist_ok=True)
    source = work / 'source'
    options = {'exact_bytes': item['bytes']} if boost_canary or codec == 'avif' else {}
    download(source_url(item['repo'], item['revision'], item['path']), source, token, **options)
    raw = source.read_bytes()
    source_digest = hashlib.sha256(raw).hexdigest()
    if item.get('source_sha256') and item['source_sha256'] != source_digest:
        message = 'pinned identity' if codec == 'avif' else 'pinned repair identity'
        raise ValueError(f'image source checksum differs from {message}')
    if item.get('bytes') and len(raw) != item['bytes']:
        raise ValueError('image source byte count differs from pinned metadata')
    if codec == 'avif' and item.get('git_blob'):
        git = hashlib.sha1(f'blob {len(raw)}\0'.encode())
        git.update(raw)
        if git.hexdigest() != item['git_blob']:
            raise ValueError('image source differs from pinned Git blob')
    del raw
    if codec == 'avif' and source_digest == previous_source_sha256:
        raise ValueError('source bytes are unchanged; historical profile conversion is disabled')
    output = work / f'document.{codec}'
    with Image.open(source) as image:
        if image.width < 1 or image.height < 1:
            raise ValueError("image has invalid dimensions")
        if codec == 'avif' and image.format != avif.FORMATS[extension]:
            raise ValueError('decoded image format differs from canaried source extension')
        frames = int(getattr(image, 'n_frames', 1) or 1) if extension in avif.TIFF or codec == 'avif' else 1
        if extension in avif.TIFF and frames > 1:
            if codec == 'avif' and frames > max_tiff_pages:
                raise ValueError('AVIF TIFF exceeds the canary page count limit')
            return build_pages(item, source, image, work, extension, source_digest, bucket,
                               legacy_source_addressed=legacy_source_addressed, codec=codec)
        if codec == 'avif' and frames != 1:
            raise ValueError('animated images are outside the AVIF still-image canary')
        # GIF (and other animated inputs in the ordinary lane) keeps the
        # production first-frame behavior, including transparent GIF palettes.
        try:
            image.seek(0)
        except EOFError:
            pass
        mode = 'RGBA' if image.format == 'GIF' and 'transparency' in image.info else 'RGB'
        encode_frame(image, output, codec=codec, webp_mode=mode)
    data = output.read_bytes()
    checksum = hashlib.sha256(data).hexdigest()
    root = (f'{IMAGE_ROOT}/{extension}/{source_digest}' if legacy_source_addressed else
            image_root(extension, source_digest, {output.name: checksum}))
    remote = f'{root}/{output.name}'
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
        "sha256": checksum,
        "page_count": 1,
        **({'profile': avif.PROFILE} if codec == 'avif' else {}),
    }, {remote: str(output)}


def build_pages(item, source, image, work, extension, source_digest, bucket, *,
                legacy_source_addressed=False, codec='webp'):
    """Publish every TIFF frame as an image page stream, never a PDF wrapper."""
    if codec == 'avif' and not 2 <= image.n_frames <= avif.MAX_PAGES:
        raise ValueError('AVIF TIFF exceeds the Reader page count limit')
    pages = work / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    entries, local_pages = [], {}
    total_bytes = 0
    for number in range(1, int(image.n_frames) + 1):
        image.seek(number - 1)
        target = pages / f'page-{number:06d}.{codec}'
        encode_frame(image, target, codec=codec)
        payload = target.read_bytes()
        total_bytes += len(payload)
        if codec == 'avif' and total_bytes > avif.MAX_BUNDLE_BYTES:
            raise ValueError('AVIF TIFF exceeds the Reader canary bundle byte limit')
        relative = f'pages/{target.name}'
        entries.append({"path": relative, "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest()})
        local_pages[relative] = str(target)
    manifest = {"kind": "image-page-stream", "version": 1, "source_extension": extension,
                "source_sha256": source_digest, "page_count": len(entries), "pages": entries,
                **({'image_format': 'avif'} if codec == 'avif' else {})}
    manifest_file = work / "page-manifest.json"
    manifest_raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()
    if codec == 'avif' and (len(manifest_raw) > avif.MAX_MANIFEST_BYTES
                             or total_bytes + len(manifest_raw) > avif.MAX_BUNDLE_BYTES):
        raise ValueError('AVIF TIFF manifest exceeds the Reader byte limit')
    manifest_file.write_bytes(manifest_raw)
    identities = {entry['path']: entry['sha256'] for entry in entries}
    identities['page-manifest.json'] = hashlib.sha256(manifest_raw).hexdigest()
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
        "bytes": total_bytes,
        **({'profile': avif.PROFILE, 'image_format': 'avif'} if codec == 'avif' else {}),
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
            if avif.keeps_existing_profile(old, entry):
                continue
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
    options = {"boost_canary": True} if getattr(args, "boost_canary", False) else {}
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
    existing_avif = {e['key']: e.get('source_sha256') for e in previous['files'] if avif.is_avif_entry(e)}
    with tempfile.TemporaryDirectory(prefix="reader-image-v2-") as directory:
        work_root = Path(directory)
        for number, item in enumerate(selected):
            folder = work_root / str(number)
            folder.mkdir()
            try:
                result, files = build_one(item, folder / 'bundle', token, args.bucket, **options)
                if result['key'] in existing_avif and result.get('source_sha256') == existing_avif[result['key']]:
                    # The LFS fast path is not available for pinned Git blobs.
                    # Confirmed equal bytes are not a reason to downgrade AVIF.
                    continue
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
