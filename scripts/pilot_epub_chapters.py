#!/usr/bin/env python3
"""Build and publish incremental EPUB chapter streams to Reader Assets v2."""

from __future__ import annotations

import argparse
import html as html_lib
import urllib.error
import time
import re
import email.utils
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from html.parser import HTMLParser
from pathlib import Path

from huggingface_hub import HfFileSystem

try:
    from .convert_reader_assets import convert_item, decode_html_source, sanitize_html
    from .epub_chapters import build_bundle, bundle_version
    from .pilot_office_stream import (
        converted_output, doc_to_html_fallback, looks_like_html, run_libreoffice,
    )
    from .reader_bucket import index_path, read_bytes
    from .recover_chm import recover as recover_chm
    from . import reader_lifecycle
    from .reader_assets import (
        bucket_conversion_contract, decode_search_payload, relative_path, source_password, source_url,
    )
    from .shared import batch_bucket_files_with_retry
except ImportError:
    from convert_reader_assets import convert_item, decode_html_source, sanitize_html
    from epub_chapters import build_bundle, bundle_version
    from pilot_office_stream import (
        converted_output, doc_to_html_fallback, looks_like_html, run_libreoffice,
    )
    from reader_bucket import index_path, read_bytes
    from recover_chm import recover as recover_chm
    import reader_lifecycle
    from reader_assets import (
        bucket_conversion_contract, decode_search_payload, relative_path, source_password, source_url,
    )
    from shared import batch_bucket_files_with_retry


TARGET_BUCKET = "vomebook/reader-assets-v2"
ROOT = "chapters/ebook/epub"
SUPPORTED_EXTENSIONS = {"epub", "mobi", "azw3", "fb2", "chm", "doc", "docx", "odt", "rtf"}
OFFICE_EXTENSIONS = {"doc", "docx", "odt", "rtf"}
OFFICE_ROOT = "chapters/ebook/epub"
OFFICE_PROFILE = "office-epub-chapters-v1-flow-512"
CHM_PROFILE = "chm-epub-chapters-v9-source-text"
EPUB_PROFILE = "epub-chapters-v8-bucket"
OFFICE_FLOW_SIZE_KB = 512
OFFICE_COMMAND_TIMEOUT_SECONDS = 900
OFFICE_LARGE_DOC_BYTES = 3 * 1024 * 1024
OFFICE_LARGE_DOC_TIMEOUT_SECONDS = 3600
OFFICE_MAX_CHAPTER_BYTES = 7 * 1024 * 1024
OFFICE_MAX_TOTAL_CHAPTER_BYTES = 500 * 1024 * 1024
OFFICE_MAX_CHAPTERS = 10_000
OFFICE_MAX_SEARCH_INDEX_BYTES = 64 * 1024 * 1024
OFFICE_MAX_SEARCH_INDEX_EXPANDED_BYTES = 256 * 1024 * 1024
SOURCE_RETRY_DELAYS = (1, 2, 4, 8, 16, 32, 64, 120)
SOURCE_TRANSIENT_HTTP_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})
SOURCE_RETRY_AFTER_CAP_SECONDS = 300


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--search-data", type=Path, default=Path("output/search_data.json"))
    parser.add_argument("--revisions", type=Path, default=Path("state/commits.json"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max-source-bytes", type=int, default=0)
    parser.add_argument("--retry-failures", action="store_true",
                         help="include previously failed sources in this extension's work queue")
    parser.add_argument("--failed-only", action="store_true",
                         help="retry only sources present in the previous failure checkpoint")
    parser.add_argument("--extension", default="epub")
    parser.add_argument("--bucket", default=TARGET_BUCKET)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan-only", action="store_true",
                        help="report stale chapter sources without downloading or converting them")
    parser.add_argument("--workers", type=int, default=1,
                        help="parallel per-book conversions (1-4; publication remains one checkpoint)")
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    return parser.parse_args()


def download(url: str, target: Path, token: str | None) -> None:
    for attempt in range(len(SOURCE_RETRY_DELAYS) + 1):
        request = urllib.request.Request(url)
        request.add_header("User-Agent", "VoiceOfML-Reader-CHM/1.0")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                payload = response.read()
            target.write_bytes(payload)
            return
        except urllib.error.HTTPError as error:
            if error.code not in SOURCE_TRANSIENT_HTTP_CODES or attempt >= len(SOURCE_RETRY_DELAYS):
                raise
            retry_after = error.headers.get("Retry-After") if error.headers else None
            delay = None
            if retry_after:
                try:
                    delay = int(float(retry_after))
                except (OverflowError, TypeError, ValueError):
                    try:
                        retry_at = email.utils.parsedate_to_datetime(retry_after)
                        delay = int(retry_at.timestamp() - time.time())
                    except (OverflowError, TypeError, ValueError):
                        pass
            if delay is None:
                try:
                    body = error.read(8192).decode("utf-8", "replace")
                except Exception:
                    body = ""
                match = re.search(r"\bretry\s+after\s+(\d+(?:\.\d+)?)\s+seconds\b",
                                  body, re.IGNORECASE)
                delay = int(float(match.group(1))) if match else SOURCE_RETRY_DELAYS[attempt]
            delay = max(1, min(SOURCE_RETRY_AFTER_CAP_SECONDS, delay))
            print(f"transient CHM source download ({error.code}); retrying in {delay}s "
                  f"(attempt {attempt + 1}/{len(SOURCE_RETRY_DELAYS)})", flush=True)
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            if attempt >= len(SOURCE_RETRY_DELAYS):
                raise
            delay = SOURCE_RETRY_DELAYS[attempt]
            print(f"transient CHM source download ({error}); retrying in {delay}s "
                  f"(attempt {attempt + 1}/{len(SOURCE_RETRY_DELAYS)})", flush=True)
            time.sleep(delay)
    raise RuntimeError("CHM source download retry limit reached")


def existing_index(bucket: str, token: str | None, root: str, name: str = "index.json") -> dict:
    try:
        fs = HfFileSystem(token=token)
        with fs.open(f"hf://buckets/{bucket}/{root}/{name}", "rb") as stream:
            payload = json.loads(stream.read().decode("utf-8"))
    except FileNotFoundError:
        return {"version": 1, "kind": "ebook-chapter-stream-index", "files": []}
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
        raise ValueError(f"invalid ebook chapter index: {bucket}/{root}/{name}")
    return payload


def merge_index_entries(previous: dict, entries: list[dict]) -> dict[str, dict]:
    """Merge source keys without dropping other EPUB-family extensions."""
    merged = {entry.get("key"): entry for entry in previous.get("files", [])
              if isinstance(entry, dict) and entry.get("key")}
    merged.update({entry["key"]: entry for entry in entries})
    return merged


def completed_revisions(index: dict, retry_failures: bool = False, profile: str | None = None) -> dict[str, str]:
    completed = {item["key"]: item.get("source_revision") for item in index.get("files", [])
                 if isinstance(item, dict) and item.get("key")
                 and (profile is None or item.get("profile") == profile)}
    if not retry_failures:
        for item in index.get("failures", []):
            if (isinstance(item, dict) and item.get("key")
                    and (profile is None or item.get("profile") == profile)):
                completed.setdefault(item["key"], item.get("source_revision"))
    return completed


def failed_keys(index: dict) -> set[str]:
    return {item.get("key") for item in index.get("failures", [])
            if isinstance(item, dict) and item.get("key")}


def chapter_profile(extension: str) -> str:
    if extension == "chm":
        return CHM_PROFILE
    if extension in OFFICE_EXTENSIONS:
        return OFFICE_PROFILE
    return EPUB_PROFILE


def merge_index_failures(previous: dict, entries: list[dict], failures: list[dict]) -> list[dict]:
    merged = {item["key"]: item for item in previous.get("failures", [])
              if isinstance(item, dict) and item.get("key")}
    for entry in entries:
        merged.pop(entry["key"], None)
    for failure in failures:
        merged[failure["key"]] = failure
    return [merged[key] for key in sorted(merged)]


def merged_index_payload(previous: dict, entries: list[dict], failures: list[dict]) -> dict:
    merged = merge_index_entries(previous, entries)
    return {"version": 1, "kind": "ebook-chapter-stream-index",
            "files": [merged[key] for key in sorted(merged)],
            "failures": merge_index_failures(previous, entries, failures)}


def processing_record_for_entry(entry: dict, bucket: str, run: dict) -> tuple[str, dict]:
    processing_id = hashlib.sha256(
        f"{entry['key']}\0{entry['source_sha256']}\0{entry['root']}".encode()
    ).hexdigest()[:32]
    processing_path = index_path(f"processing/office-chapters-{processing_id}.json")
    record = reader_lifecycle.processing_record(
        [{"bucket": bucket, "root": entry["root"]}], "office-chapter-upload", run)
    return processing_path, record


def shard_for(key: str, count: int) -> int:
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big") % count


def decrypt_office_source(source: Path, extension: str, password: str, work: Path) -> Path:
    """Decrypt a password-protected DOC/DOCX before offline conversion.

    The filename-encoded source password (for example ``〔密码：123〕``) and the
    known-password table already drive the native Reader conversion path. Without
    this step LibreOffice silently produces no output for encrypted inputs, so the
    office chapter stream must decrypt first and convert the plaintext copy.
    """
    if not password or extension not in {"doc", "docx"}:
        return source
    import msoffcrypto
    with source.open("rb") as encrypted:
        try:
            document = msoffcrypto.OfficeFile(encrypted)
        except Exception:
            return source
        if not document.is_encrypted():
            return source
        decrypted = work / f"decrypted.{extension}"
        document.load_key(password=password)
        with decrypted.open("wb") as handle:
            document.decrypt(handle)
    return decrypted


class _LegacyOfficeText(HTMLParser):
    """Extract text from the native DOC fallback without retaining its <pre>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "pre":
            self.depth += 1

    def handle_endtag(self, tag):
        if tag.lower() == "pre" and self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)


def write_styled_legacy_office_html(raw: bytes, output: Path, title: str) -> None:
    """Turn the old native text fallback into readable, styled HTML.

    The legacy converter stores difficult OLE DOC files as one giant ``<pre>``.
    Feeding that directly to the chapter converter produces the ugly native HTML
    view. Reflow its blank-line-separated text into normal paragraphs first.
    """
    parser = _LegacyOfficeText()
    parser.feed(raw.decode("utf-8", "replace"))
    parser.close()
    text = "\n".join(parser.parts)
    if not text.strip():
        raise ValueError("legacy office HTML contains no readable text")
    paragraphs = []
    for block in re.split(r"\n[ \t]*\n+", text.replace("\r\n", "\n")):
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in block.split("\n")]
        value = " ".join(line for line in lines if line)
        if not value:
            continue
        value = value.replace("[pic]", "（图片）")
        paragraphs.append(f"<p>{html_lib.escape(value)}</p>")
    if not paragraphs:
        raise ValueError("legacy office HTML contains no non-empty paragraphs")
    style = (
        "body{margin:0 auto;max-width:48rem;padding:1.5rem 1.25rem;"
        "font-family:serif;font-size:1rem;line-height:1.8;color:#202124;}"
        "h1{font-size:1.55rem;line-height:1.35;text-align:center;margin:0 0 2rem;}"
        "p{margin:0 0 .9em;text-indent:2em;text-align:justify;}"
        "p:first-of-type{text-indent:0;}"
        "@media(prefers-color-scheme:dark){body{color:#e7e9eb;background:#181b1e;}}"
    )
    document = ("<!doctype html><html><head><meta charset=\"utf-8\"><title>"
                f"{html_lib.escape(title)}</title><style>{style}</style></head><body>"
                f"<h1>{html_lib.escape(title)}</h1>{''.join(paragraphs)}</body></html>")
    output.write_text(document, encoding="utf-8")


def office_to_epub(source: Path, extension: str, work: Path, password: str = "",
                   legacy_html: bytes | None = None, title: str = "") -> Path:
    """Normalize office input once, then emit bounded-flow EPUB chapters.

    DOCX is a ZIP container and cannot be opened meaningfully through HTTP
    ranges. The conversion is therefore an offline publication step; the
    browser later requests only the generated XHTML chapter bodies.
    """
    if extension not in OFFICE_EXTENSIONS:
        raise ValueError(f"unsupported office chapter extension: {extension}")
    source = decrypt_office_source(source, extension, password, work)
    epub = work / "document.epub"
    html_doc = extension == "doc" and looks_like_html(source)
    large_legacy_doc = (
        extension == "doc" and not html_doc and source.stat().st_size >= OFFICE_LARGE_DOC_BYTES
    )
    timeout_seconds = (OFFICE_LARGE_DOC_TIMEOUT_SECONDS if large_legacy_doc
                       else OFFICE_COMMAND_TIMEOUT_SECONDS)

    def convert_to_epub(input_path: Path) -> None:
        subprocess.run(
            ["ebook-convert", str(input_path), str(epub), "--flow-size", str(OFFICE_FLOW_SIZE_KB)],
            check=True, timeout=timeout_seconds,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        if not epub.is_file() or epub.stat().st_size <= 0:
            raise RuntimeError("office EPUB conversion produced no output")

    if legacy_html is not None:
        html_source = work / "legacy-native.html"
        write_styled_legacy_office_html(legacy_html, html_source, title or source.stem)
        convert_to_epub(html_source)
        return epub

    # Some historical .doc files are HTML documents with a misleading suffix.
    # Decode and sanitize them as HTML rather than asking LibreOffice to parse
    # them as OLE documents (which can exit successfully without producing DOCX).
    if html_doc:
        html_source = work / "source.html"
        html_source.write_text(
            sanitize_html(decode_html_source(source), allow_relative=True), encoding="utf-8")
        convert_to_epub(html_source)
        return epub

    try:
        normalized = source
        if extension != "docx" or not zipfile.is_zipfile(source):
            output = work / "normalized-docx"
            output.mkdir(parents=True, exist_ok=True)
            run_libreoffice(
                ["libreoffice", "--headless", "--convert-to", "docx", "--outdir", str(output), str(source)],
                work / "libreoffice-profile",
                timeout_seconds=(OFFICE_LARGE_DOC_TIMEOUT_SECONDS if large_legacy_doc else None),
            )
            normalized = converted_output(output, "docx")
        convert_to_epub(normalized)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError,
            RuntimeError) as primary_error:
        if extension != "doc" or large_legacy_doc:
            raise
        # Keep conversion of large/legacy binary DOC files complete when the
        # layout-preserving LibreOffice/Calibre path times out. This is the same
        # antiword text fallback used by the native DOC Reader conversion.
        text_html = work / "antiword-fallback.html"
        doc_to_html_fallback(source, text_html)
        epub.unlink(missing_ok=True)
        try:
            convert_to_epub(text_html)
        except Exception as fallback_error:
            raise RuntimeError(
                f"DOC conversion failed ({type(primary_error).__name__}: {primary_error}); "
                f"antiword EPUB fallback failed ({type(fallback_error).__name__}: {fallback_error})"
            ) from fallback_error
    return epub


def validate_office_search_index_expanded_size(expanded_bytes: int) -> None:
    if expanded_bytes > OFFICE_MAX_SEARCH_INDEX_EXPANDED_BYTES:
        raise ValueError("office search index exceeds the bounded Reader Worker text limit")


def validate_office_gzip_search_index(path: Path) -> int:
    expanded_bytes = 0
    with gzip.open(path, "rb") as stream:
        while chunk := stream.read(1024 * 1024):
            expanded_bytes += len(chunk)
            validate_office_search_index_expanded_size(expanded_bytes)
    return expanded_bytes


def validate_office_chapter_budget(manifest: dict, bundle: Path | None = None) -> None:
    chapters = manifest.get("chapters", [])
    search_index_bytes = int((manifest.get("search_index") or {}).get("bytes") or 0)
    if (not isinstance(chapters, list) or len(chapters) > OFFICE_MAX_CHAPTERS
            or sum(int(chapter.get("bytes") or 0) for chapter in chapters) > OFFICE_MAX_TOTAL_CHAPTER_BYTES
            or search_index_bytes > OFFICE_MAX_SEARCH_INDEX_BYTES):
        raise ValueError("office chapter stream exceeds the Reader manifest/search budget")
    if any(int(chapter.get("bytes") or 0) > OFFICE_MAX_CHAPTER_BYTES for chapter in chapters):
        raise ValueError("office chapter exceeds the bounded browser chapter limit")
    if bundle is not None:
        search_path = (manifest.get("search_index") or {}).get("path")
        if search_path != "epub-search-index.json.gz":
            raise ValueError("office bundle has an unexpected search-index path")
        validate_office_gzip_search_index(bundle / search_path)


def select_records(path: Path, revisions: dict, max_bytes: int, extension_filter: str) -> list[dict]:
    records = decode_search_payload(json.loads(path.read_text(encoding="utf-8")))
    selected = []
    for record in records:
        extension = str(record.get("Extension") or "").lower().lstrip(".")
        size = int(record.get("Size") or 0)
        if extension != extension_filter or (max_bytes and size > max_bytes):
            continue
        repo = str(record.get("Repo") or "")
        revision = str(revisions.get(repo) or "")
        if repo and revision:
            selected.append({"repo": repo, "path": relative_path(record), "revision": revision, "source_bytes": size})
    selected.sort(key=lambda item: (item["repo"], item["path"]))
    return selected


def build_one(item: dict, work: Path, token: str | None, bucket: str) -> tuple[dict, dict[str, str]]:
    work.mkdir(parents=True, exist_ok=True)
    extension = item["extension"]
    source = work / f"source.{extension}"
    download(source_url(item["repo"], item["revision"], item["path"]), source, token)
    source_bytes = source.read_bytes()
    source_digest = hashlib.sha256(source_bytes).hexdigest()
    native_fallback = None
    if extension in OFFICE_EXTENSIONS:
        legacy_html = None
        if extension == "doc":
            try:
                legacy_html = read_bytes(
                    f"documents/office/doc/{source_digest}/document.html", token, bucket)
                print(f"using legacy native DOC fallback for {item['repo']}/{item['path']}")
            except FileNotFoundError:
                pass
        chapter_source = office_to_epub(
            source, extension, work, source_password(item["repo"], item["path"]),
            legacy_html=legacy_html, title=Path(item["path"]).stem)
        bundle = work / "bundle"
        manifest = build_bundle(chapter_source, bundle)
        validate_office_chapter_budget(manifest, bundle)
        root_extension = "epub"
    elif extension == "chm":
        contract = bucket_conversion_contract(item["repo"], item["path"], extension, item["source_bytes"])
        if contract is None:
            raise ValueError("no CHM conversion contract")
        profile, mode, output_name = contract
        converted_root = work / "converted"
        try:
            converted = convert_item({
                "key": f"{item['repo']}\0{item['path']}", "repo": item["repo"], "path": item["path"],
                "extension": extension, "source_revision": item["revision"], "source_url": source_url(
                    item["repo"], item["revision"], item["path"]), "source_bytes": item["source_bytes"],
                "profile": profile, "reader_mode": mode, "output_name": output_name,
            }, converted_root)
            if not converted.get("chapter_manifest") or converted.get("chapter_bundle_error"):
                raise RuntimeError(converted.get("chapter_bundle_error") or "CHM chapter stream was not produced")
            source_chapter_dir = converted_root / Path(converted["chapter_manifest"]).parent
            native_source = converted_root / converted["path"]
            bundle = work / "bundle"
            shutil.copytree(source_chapter_dir, bundle)
            manifest = json.loads((bundle / "chapter-manifest.json").read_text(encoding="utf-8"))
        except Exception as primary_error:
            # Calibre and its CHM navigation repair are not reliable for every
            # legacy container. Recover static pages directly, then feed the
            # repaired EPUB through the same chapter-stream builder.
            recovered = work / "recovered.epub"
            try:
                recover_chm(source, recovered, Path(item["path"]).stem)
                bundle = work / "bundle"
                manifest = build_bundle(recovered, bundle)
                native_source = recovered
                print(f"CHM recovery fallback succeeded: {item['repo']}/{item['path']}")
            except Exception as recovery_error:
                raise RuntimeError(
                    f"CHM conversion failed ({type(primary_error).__name__}: {primary_error}); "
                    f"recovery failed ({type(recovery_error).__name__}: {recovery_error})"
                ) from recovery_error
        native_fallback = f"native/ebook/chm/{source_digest}/document.epub"
        root_extension = extension
    else:
        chapter_source = source
        if extension != "epub":
            chapter_source = work / "chapter-source.epub"
            subprocess.run(["ebook-convert", str(source), str(chapter_source), "--flow-size", "0"],
                           check=True, timeout=600)
        bundle = work / "bundle"
        manifest = build_bundle(chapter_source, bundle)
        root_extension = extension
    if extension == "chm":
        version = bundle_version(bundle)
        root_extension = extension
        root = f"chapters/ebook/{root_extension}/{source_digest}/{version}"
    else:
        version = bundle_version(bundle)
        root = f"chapters/ebook/{root_extension}/{source_digest}/{version}"
    uploads = {}
    for path in sorted(item for item in bundle.rglob("*") if item.is_file()):
        uploads[f"{root}/{path.relative_to(bundle).as_posix()}"] = str(path)
    if native_fallback:
        uploads[native_fallback] = str(native_source)
    return {
        "key": f"{item['repo']}\0{item['path']}", "repo": item["repo"], "path": item["path"],
        "source_extension": extension, "extension": root_extension, "source_revision": item["revision"],
        "source_sha256": source_digest,
        "source_bytes": len(source_bytes), "mode": "chapter-stream", "bucket": bucket,
        "profile": chapter_profile(extension),
        "root": root, "manifest": f"{root}/chapter-manifest.json",
        "chapter_count": len(manifest["chapters"]),
        "search_index": f"{root}/epub-search-index.json.gz",
        **({"fallback": native_fallback, "object": native_fallback} if native_fallback else {}),
    }, uploads


def main() -> int:
    args = parse_args()
    if args.limit < 0 or args.max_source_bytes < 0:
        raise ValueError("limit and max-source-bytes must be non-negative")
    if not 1 <= args.workers <= 4:
        raise ValueError("workers must be between 1 and 4")
    if args.shard_count < 1 or not 0 <= args.shard_index < args.shard_count:
        raise ValueError("invalid shard")
    token = os.environ.get("HF_TOKEN")
    extension = args.extension.lower().lstrip(".")
    if extension not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"unsupported ebook extension: {extension}")
    revisions = json.loads(args.revisions.read_text(encoding="utf-8"))
    root_prefix = OFFICE_ROOT if extension in OFFICE_EXTENSIONS else f"chapters/ebook/{extension}"
    index_name = "index.json" if args.shard_count == 1 else f"index-{args.shard_index:02d}.json"
    previous = existing_index(args.bucket, token, root_prefix, index_name)
    if args.failed_only and not args.retry_failures:
        raise ValueError("--failed-only requires --retry-failures")
    expected_profile = chapter_profile(extension)
    failure_keys = failed_keys(previous)
    completed = completed_revisions(
        previous, args.retry_failures, expected_profile if extension == "chm" else None)
    selected = [item for item in select_records(args.search_data, revisions, args.max_source_bytes, extension)
                if shard_for(f"{item['repo']}\0{item['path']}", args.shard_count) == args.shard_index
                if not args.failed_only or f"{item['repo']}\0{item['path']}" in failure_keys
                if completed.get(f"{item['repo']}\0{item['path']}") != item["revision"]]
    if args.limit:
        selected = selected[:args.limit]
    if args.plan_only:
        print(json.dumps({"kind": "epub-chapter-plan", "extension": extension,
                          "profile": expected_profile, "selected": len(selected)}, ensure_ascii=False), flush=True)
        return 0
    if not selected:
        print("no pending chapter sources")
        return 0
    uploads, entries, failures = {}, [], []
    with tempfile.TemporaryDirectory(prefix="reader-epub-") as directory:
        root = Path(directory)

        def build_selected(pair):
            index, item = pair
            item["extension"] = extension
            try:
                return item, *build_one(item, root / str(index), token, args.bucket), None
            except Exception as error:
                failure = {
                    "key": f"{item['repo']}\0{item['path']}",
                    "repo": item["repo"], "path": item["path"],
                    "source_revision": item["revision"],
                    "profile": expected_profile,
                    "error": f"{type(error).__name__}: {error}",
                }
                return item, None, None, failure

        pairs = list(enumerate(selected))
        if args.workers == 1:
            built = map(build_selected, pairs)
            executor = None
        else:
            executor = ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix="chm-convert")
            built = executor.map(build_selected, pairs)
        try:
            for item, entry, files, failure in built:
                if failure:
                    failures.append(failure)
                    print(f"failed: {item['repo']}/{item['path']}: {failure['error']}", flush=True)
                    continue
                entries.append(entry)
                uploads.update(files)
        finally:
            if executor:
                executor.shutdown(wait=True, cancel_futures=True)
        merged = merge_index_entries(previous, entries)
        index_file = root / "index.json"
        payload = merged_index_payload(previous, entries, failures)
        index_file.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                              encoding="utf-8")
        processing_uploads = []
        if args.apply:
            for entry in entries:
                processing_path, record = processing_record_for_entry(
                    entry, args.bucket,
                    {"repository": os.environ.get("GITHUB_REPOSITORY", ""),
                     "run_id": os.environ.get("GITHUB_RUN_ID", ""),
                     "attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "")})
                local_record = root / "processing-roots" / Path(processing_path).name
                local_record.parent.mkdir(parents=True, exist_ok=True)
                local_record.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True,
                                                   separators=(",", ":")) + "\n", encoding="utf-8")
                processing_uploads.append((str(local_record), processing_path))
                entry["processing_roots"] = [processing_path]
                merged[entry["key"]] = entry
            payload = {"version": 1, "kind": "ebook-chapter-stream-index",
                       "files": [merged[key] for key in sorted(merged)],
                       "failures": merge_index_failures(previous, entries, failures)}
            index_file.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                                  encoding="utf-8")
            if processing_uploads:
                batch_bucket_files_with_retry(args.bucket, processing_uploads, token)
        uploads[f"{root_prefix}/{index_name}"] = str(index_file)
        print(f"planned {len(entries)} EPUB stream(s), {len(uploads)} object(s), failures={len(failures)}")
        if args.apply:
            batch_bucket_files_with_retry(args.bucket, [(local, remote) for remote, local in sorted(uploads.items())], token)
            print(f"published {len(uploads)} object(s) to {args.bucket}")
        else:
            print("report-only; pass --apply to publish")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
