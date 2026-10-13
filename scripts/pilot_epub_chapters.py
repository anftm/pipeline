#!/usr/bin/env python3
"""Build and publish incremental EPUB chapter streams to Reader Assets v2."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from huggingface_hub import HfFileSystem

try:
    from .convert_reader_assets import convert_item
    from .epub_chapters import build_bundle, bundle_version
    from .pilot_office_stream import converted_output, run_libreoffice
    from .reader_bucket import index_path
    from .recover_chm import recover as recover_chm
    from . import reader_lifecycle
    from .reader_assets import (
        bucket_conversion_contract, decode_search_payload, relative_path, source_password, source_url,
    )
    from .shared import batch_bucket_files_with_retry
except ImportError:
    from convert_reader_assets import convert_item
    from epub_chapters import build_bundle, bundle_version
    from pilot_office_stream import converted_output, run_libreoffice
    from reader_bucket import index_path
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
OFFICE_FLOW_SIZE_KB = 512
OFFICE_COMMAND_TIMEOUT_SECONDS = 900
OFFICE_MAX_CHAPTER_BYTES = 7 * 1024 * 1024
OFFICE_MAX_TOTAL_CHAPTER_BYTES = 500 * 1024 * 1024
OFFICE_MAX_CHAPTERS = 10_000
OFFICE_MAX_SEARCH_INDEX_BYTES = 64 * 1024 * 1024
OFFICE_MAX_SEARCH_INDEX_EXPANDED_BYTES = 256 * 1024 * 1024


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--search-data", type=Path, default=Path("output/search_data.json"))
    parser.add_argument("--revisions", type=Path, default=Path("state/commits.json"))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max-source-bytes", type=int, default=0)
    parser.add_argument("--retry-failures", action="store_true",
                         help="include previously failed sources in this extension's work queue")
    parser.add_argument("--extension", default="epub")
    parser.add_argument("--bucket", default=TARGET_BUCKET)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    return parser.parse_args()


def download(url: str, target: Path, token: str | None) -> None:
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=180) as response:
        target.write_bytes(response.read())


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


def completed_revisions(index: dict, retry_failures: bool = False) -> dict[str, str]:
    completed = {item["key"]: item.get("source_revision") for item in index.get("files", [])
                 if isinstance(item, dict) and item.get("key")}
    if not retry_failures:
        for item in index.get("failures", []):
            if isinstance(item, dict) and item.get("key"):
                completed.setdefault(item["key"], item.get("source_revision"))
    return completed


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


def office_to_epub(source: Path, extension: str, work: Path, password: str = "") -> Path:
    """Normalize office input once, then emit bounded-flow EPUB chapters.

    DOCX is a ZIP container and cannot be opened meaningfully through HTTP
    ranges. The conversion is therefore an offline publication step; the
    browser later requests only the generated XHTML chapter bodies.
    """
    if extension not in OFFICE_EXTENSIONS:
        raise ValueError(f"unsupported office chapter extension: {extension}")
    source = decrypt_office_source(source, extension, password, work)
    normalized = source
    if extension != "docx" or not zipfile.is_zipfile(source):
        output = work / "normalized-docx"
        output.mkdir(parents=True, exist_ok=True)
        run_libreoffice(
            ["libreoffice", "--headless", "--convert-to", "docx", "--outdir", str(output), str(source)],
            work / "libreoffice-profile",
        )
        normalized = converted_output(output, "docx")
    epub = work / "document.epub"
    subprocess.run(
        ["ebook-convert", str(normalized), str(epub), "--flow-size", str(OFFICE_FLOW_SIZE_KB)],
        check=True, timeout=OFFICE_COMMAND_TIMEOUT_SECONDS,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if not epub.is_file() or epub.stat().st_size <= 0:
        raise RuntimeError("office EPUB conversion produced no output")
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
        chapter_source = office_to_epub(
            source, extension, work, source_password(item["repo"], item["path"]))
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
        "profile": OFFICE_PROFILE if extension in OFFICE_EXTENSIONS else "epub-chapters-v8-bucket",
        "root": root, "manifest": f"{root}/chapter-manifest.json",
        "chapter_count": len(manifest["chapters"]),
        "search_index": f"{root}/epub-search-index.json.gz",
        **({"fallback": native_fallback, "object": native_fallback} if native_fallback else {}),
    }, uploads


def main() -> int:
    args = parse_args()
    if args.limit < 0 or args.max_source_bytes < 0:
        raise ValueError("limit and max-source-bytes must be non-negative")
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
    completed = completed_revisions(previous, args.retry_failures)
    selected = [item for item in select_records(args.search_data, revisions, args.max_source_bytes, extension)
                if shard_for(f"{item['repo']}\0{item['path']}", args.shard_count) == args.shard_index
                if completed.get(f"{item['repo']}\0{item['path']}") != item["revision"]]
    if args.limit:
        selected = selected[:args.limit]
    if not selected:
        print("no pending chapter sources")
        return 0
    uploads, entries, failures = {}, [], []
    with tempfile.TemporaryDirectory(prefix="reader-epub-") as directory:
        root = Path(directory)
        for index, item in enumerate(selected):
            item["extension"] = extension
            try:
                entry, files = build_one(item, root / str(index), token, args.bucket)
            except Exception as error:
                failures.append({
                    "key": f"{item['repo']}\0{item['path']}",
                    "repo": item["repo"], "path": item["path"],
                    "source_revision": item["revision"],
                    "error": f"{type(error).__name__}: {error}",
                })
                print(f"failed: {item['repo']}/{item['path']}: {type(error).__name__}: {error}")
                continue
            entries.append(entry)
            uploads.update(files)
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
