#!/usr/bin/env python3
"""Convert selected non-OCR source formats into static PDF Reader streams."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone
import urllib.error
import urllib.request
from pathlib import Path

try:
    from .convert_reader_assets import convert_tiff, validate_pdf_content
    from .pilot_office_stream import converted_output, run_libreoffice
    from .reader_assets import decode_search_payload, relative_path, source_url
    from .reader_stream_index import publish_checkpoint, read_index
    from .shared import hash_file as stream_file_hash
    from . import office_symbol_fonts
except ImportError:
    from convert_reader_assets import convert_tiff, validate_pdf_content
    from pilot_office_stream import converted_output, run_libreoffice
    from reader_assets import decode_search_payload, relative_path, source_url
    from reader_stream_index import publish_checkpoint, read_index
    from shared import hash_file as stream_file_hash
    import office_symbol_fonts


BUCKET = "vomebook/reader-assets-v2"
EXTENSIONS = {"ppt", "pptx", "pps", "wps", "ps"}
DOWNLOAD_INTERVAL_SECONDS = 0.35
DOWNLOAD_ATTEMPTS = 5
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--search-data", type=Path, default=Path("output/search_data.json"))
    parser.add_argument("--revisions", type=Path, default=Path("state/commits.json"))
    parser.add_argument("--extension", default="all")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--bucket", default=BUCKET)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--retry-failures", action="store_true")
    parser.add_argument("--strict", action="store_true",
                        help="return a failure after checkpointing any source errors")
    return parser.parse_args()


def retry_delay(error: urllib.error.HTTPError, attempt: int) -> float:
    headers = error.headers or {}
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
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    for attempt in range(DOWNLOAD_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                target.write_bytes(response.read())
            time.sleep(DOWNLOAD_INTERVAL_SECONDS)
            return
        except urllib.error.HTTPError as error:
            if error.code not in RETRYABLE_STATUS or attempt == DOWNLOAD_ATTEMPTS - 1:
                raise
            delay = retry_delay(error, attempt)
            print(f"source download HTTP {error.code}; retrying in {delay:.0f}s "
                  f"({attempt + 1}/{DOWNLOAD_ATTEMPTS})")
            time.sleep(delay)


def existing_index(bucket: str, token: str | None, root: str) -> dict:
    return read_index(bucket, f"{root}/index.json", token, "static-pdf-stream-index")


def records(path: Path, revisions: dict, extension: str) -> list[dict]:
    result = []
    for record in decode_search_payload(json.loads(path.read_text(encoding="utf-8"))):
        ext = str(record.get("Extension") or "").lower().lstrip(".")
        if ext not in EXTENSIONS or (extension != "all" and ext != extension):
            continue
        repo = str(record.get("Repo") or "")
        revision = str(revisions.get(repo) or "")
        if repo and revision:
            result.append({"repo": repo, "path": relative_path(record), "extension": ext,
                           "revision": revision})
    return sorted(result, key=lambda item: (item["extension"], item["repo"], item["path"]))


def build(item: dict, work: Path, token: str | None) -> tuple[dict, dict[str, str]]:
    work.mkdir(parents=True, exist_ok=True)
    source = work / f"source.{item['extension']}"
    download(source_url(item["repo"], item["revision"], item["path"]), source, token)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    conversion = None
    office_source = source
    if item['extension'] == 'pptx':
        office_source, conversion = office_symbol_fonts.prepare(source, work)
    output_dir = work / "pdf"
    output_dir.mkdir()
    if item["extension"] in {"tif", "tiff"}:
        output = output_dir / "document.pdf"
        convert_tiff(source, output, work)
    elif item["extension"] == "ps":
        output = output_dir / "document.pdf"
        subprocess.run(["gs", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite",
                        f"-sOutputFile={output}", str(source)], check=True, timeout=600)
    else:
        run_libreoffice(["libreoffice", "--headless", "--convert-to", "pdf",
                         "--outdir", str(output_dir), str(office_source)], work / "libreoffice-profile")
        output = converted_output(output_dir, "pdf")
    validation = work / "validation"
    validation.mkdir()
    validate_pdf_content(output, validation)
    output_bytes = output.read_bytes()
    output_hash = hashlib.sha256(output_bytes).hexdigest()
    # A changed conversion must not replace immutable PDFs under their old URL.
    category = 'pptx-symbols-v2' if item['extension'] == 'pptx' else item['extension']
    root = f"documents/pdf/{category}/{source_hash}"
    object_path = f"{root}/document.pdf"
    manifest_path = f"{root}/document-manifest.json"
    manifest = work / "document-manifest.json"
    extra_uploads = {}
    evidence = None
    if conversion:
        raw = (work/'symbol-conversion.json').read_bytes()
        evidence = {'bucket': BUCKET, 'path': root+'/symbol-conversion.json', 'sha256': hashlib.sha256(raw).hexdigest(),
                    'bytes': len(raw), 'role': 'provenance'}
        extra_uploads[evidence['path']] = str(work/'symbol-conversion.json')
    manifest.write_text(json.dumps({
        "kind": "static-pdf-document-stream", "version": 1,
        "source_extension": item["extension"], "source_sha256": source_hash,
        "path": "document.pdf", "bytes": len(output_bytes), "sha256": output_hash,
        **({'conversion_profile': office_symbol_fonts.PROFILE} if item['extension'] == 'pptx' else {}),
        **({'symbol_conversion': evidence} if evidence else {}),
    }, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    entry = {"key": f"{item['repo']}\0{item['path']}", "repo": item["repo"],
             "path": item["path"], "extension": item["extension"],
             "source_revision": item["revision"], "source_sha256": source_hash,
             "mode": "pdf", "bucket": BUCKET, "object": object_path,
              "manifest": manifest_path, "bytes": len(output_bytes), "sha256": output_hash}
    if item['extension'] == 'pptx':
        entry['conversion_profile'] = office_symbol_fonts.PROFILE
    return entry, {object_path: str(output), manifest_path: str(manifest), **extra_uploads}


def main() -> int:
    args = parse_args()
    if args.limit < 0:
        raise ValueError("limit must be non-negative")
    extension = args.extension.lower().lstrip(".")
    if extension != "all" and extension not in EXTENSIONS:
        raise ValueError(f"unsupported static PDF extension: {extension}")
    token = os.environ.get("HF_TOKEN")
    revisions = json.loads(args.revisions.read_text(encoding="utf-8"))
    selected = records(args.search_data, revisions, extension)
    entries, failures = [], {}
    with tempfile.TemporaryDirectory(prefix="reader-static-pdf-") as directory:
        root = Path(directory)
        by_category, category_uploads, planned = {}, {}, {}
        for number, item in enumerate(selected):
            category = f"documents/pdf/{item['extension']}"
            if category not in planned:
                planned[category] = existing_index(args.bucket, token, category)
            old = planned[category]
            key = f"{item['repo']}\0{item['path']}"
            handled = list(old.get("files", []))
            if not args.retry_failures:
                handled += list(old.get("failures", []))
            if any(e.get("key") == key and e.get("source_revision") == item["revision"]
                    and (item['extension'] != 'pptx' or e.get('conversion_profile') == office_symbol_fonts.PROFILE)
                   for e in handled if isinstance(e, dict)):
                continue
            attempted_at = datetime.now(timezone.utc).isoformat()
            try:
                entry, files = build(item, root / str(number), token)
            except Exception as error:
                failures[key] = {"key": key, "repo": item["repo"], "path": item["path"],
                                  "extension": item["extension"], "source_revision": item["revision"],
                                  **({'conversion_profile': office_symbol_fonts.PROFILE} if item['extension'] == 'pptx' else {}),
                                  "checkpointed_at": attempted_at,
                                  "error": f"{type(error).__name__}: {error}"[:1000]}
                print(f"failed: {item['repo']}/{item['path']}: {type(error).__name__}: {error}")
                continue
            entry['checkpointed_at'] = attempted_at
            entries.append(entry); by_category.setdefault(category, []).append(entry)
            owned = category_uploads.setdefault(category, {})
            for remote, local in files.items():
                if remote in owned and stream_file_hash(Path(owned[remote])) != stream_file_hash(Path(local)):
                    raise ValueError(f"conflicting static PDF outputs at one immutable path: {remote}")
                owned[remote] = local
            if args.limit and len(entries) >= args.limit:
                break
        for failure in failures.values():
            by_category.setdefault(f"documents/pdf/{failure['extension']}", [])
        if not entries and not failures:
            print("no pending static PDF sources")
            return 0
        for category, added in by_category.items():
            old = existing_index(args.bucket, token, category)
            category_failures = [v for v in failures.values() if f"documents/pdf/{v['extension']}" == category]
            # Each source has its own immutable processing record and exact
            # checkpoint. Reuse verified uploads for a shared source root.
            for number, entry in enumerate(added):
                current = [item for field in ('files', 'failures') for item in old.get(field, [])
                           if item.get('key') == entry['key']]
                if any(item.get('checkpointed_at', '') > entry['checkpointed_at'] for item in current):
                    print(f"skipped stale static PDF result: {entry['key']}")
                    continue
                object_path, manifest_path = entry.get('object'), entry.get('manifest')
                if not isinstance(object_path, str) or not isinstance(manifest_path, str):
                    raise ValueError('static PDF entry lacks object and manifest')
                owner = str(Path(object_path).parent)
                if str(Path(manifest_path).parent) != owner or not owner.startswith(category + '/'):
                    raise ValueError('static PDF manifest does not share its source root')
                uploads = {remote: local for remote, local in category_uploads.get(category, {}).items()
                           if str(Path(remote).parent) == owner}
                if object_path not in uploads or manifest_path not in uploads:
                    raise ValueError('static PDF source checkpoint is incomplete')
                work = root / category.replace("/", "_") / str(number)
                work.mkdir(parents=True, exist_ok=True)
                old = publish_checkpoint(args.bucket, f"{category}/index.json", old, [entry], [],
                                         uploads, work, token, apply=args.apply,
                                         kind="static-pdf-stream-index")
            if category_failures:
                work = root / category.replace("/", "_") / 'failures'
                work.mkdir(parents=True, exist_ok=True)
                publish_checkpoint(args.bucket, f"{category}/index.json", old, [], category_failures,
                                   {}, work, token, apply=args.apply, kind="static-pdf-stream-index")
        print(f"processed={len(entries)} failures={len(failures)} "
              f"objects={sum(len(files) for files in category_uploads.values())} apply={args.apply}")
        if args.strict and failures:
            print(f"strict static PDF conversion failed for {len(failures)} source(s)")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
