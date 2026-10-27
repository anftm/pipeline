#!/usr/bin/env python3
"""Build the new-only compact Reader sidecar from v2 canonical indexes."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import tempfile
from pathlib import Path

from huggingface_hub import HfFileSystem, batch_bucket_files

try:
    from . import shared
    from . import reader_lifecycle
    from .reader_bucket import publish_catalog, publish_json, read_json as read_bucket_json
except ImportError:
    import shared
    import reader_lifecycle
    from reader_bucket import publish_catalog, publish_json, read_json as read_bucket_json


ASSETS_BUCKET = "vomebook/reader-assets-v2"
PDF_BUCKET = "vomebook/pdf-pages-v2"
SIDECAR_PATH = "reader-index/reader_assets.json.gz"
EBOOK_EXTENSIONS = ("epub", "mobi", "azw3", "fb2", "chm")


def arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets-bucket", default=ASSETS_BUCKET)
    parser.add_argument("--pdf-bucket", default=PDF_BUCKET)
    parser.add_argument("--derived-manifest", default="reader-index/derived_pdf_manifest.json")
    parser.add_argument("--render-manifest", default="reader-index/pdf_render_manifest.json")
    parser.add_argument("--ocr-manifest", default="reader-index/pdf_ocr_manifest.json")
    parser.add_argument("--output", type=Path, default=Path("output/reader_assets_v2.json.gz"))
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def read_json(fs: HfFileSystem, bucket: str, path: str) -> dict:
    with fs.open(f"hf://buckets/{bucket}/{path}", "rb") as stream:
        value = json.loads(stream.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"invalid index: {bucket}/{path}")
    return value


def add_index(files: dict, payload: dict, mode_for: dict[str, str], bucket: str,
              *, chapters: bool = False) -> None:
    for entry in payload.get("files", []):
        if not isinstance(entry, dict) or not entry.get("key"):
            continue
        extension = str(entry.get("extension") or entry.get("source_extension") or "").lower()
        mode = mode_for.get(extension)
        if not mode:
            continue
        path = entry.get("manifest") if chapters else entry.get("object")
        if not isinstance(path, str):
            continue
        if mode == "d" and path.endswith("/document.html"):
            mode = "h"
        compact = {"s": 2, "m": mode, "p": path, "b": bucket}
        if chapters:
            compact.update({"c": path, "cb": bucket})
            if entry.get("fallback"):
                compact["f"] = entry["fallback"]
        files[entry["key"]] = compact


def add_pdf_streams(files: dict, payload: dict, bucket: str) -> None:
    # Document images are historical; the v3 primary-document projection is
    # built separately. Never re-advertise a stale document image route.
    return None


def release_processing_roots(indexes: list[dict], generation: str, read, publish) -> int:
    """Release only newly handed-off roots; retained released roots are read-only."""
    results = [entry for payload in indexes for entry in payload.get("files", [])
               if isinstance(entry, dict)]
    updates = reader_lifecycle.processing_handoffs(results, generation, read)
    for path, record in updates.items():
        publish(path, record)
    return sum(path.startswith('reader-index/processing/') for path in updates)


def ebook_chapter_indexes(assets_bucket: str) -> list[tuple[str, str]]:
    """Return every canonical ebook chapter index that can own upload roots."""
    return [(assets_bucket, f"chapters/ebook/{extension}/index.json")
            for extension in EBOOK_EXTENSIONS]


def stream_processing_indexes(assets_bucket: str) -> list[tuple[str, str]]:
    return [(assets_bucket, f"documents/{family}/{extension}/index.json")
            for family, extensions in (("web", ("html", "htm", "mht", "mhtml")),
                                       ("spreadsheet", ("xls", "xlsx", "csv", "ods")))
            for extension in extensions] + [(assets_bucket, f"media/{kind}/index.json")
                                           for kind in ("audio", "video", "swf")]


def build(fs: HfFileSystem, assets_bucket: str, pdf_bucket: str, derived_path: str,
          render_path: str, ocr_path: str) -> dict:
    files = {}
    for extension, mode in (("txt", "t"), ("md", "k"), ("markdown", "k"), ("vcf", "t"), ("ini", "t")):
        payload = read_json(fs, assets_bucket, "documents/text/index.json")
        if extension == "txt":
            selected = [x for x in payload.get("files", []) if x.get("extension") == extension]
            add_index(files, {"files": selected}, {extension: mode}, assets_bucket)
        elif extension == "md":
            selected = [x for x in payload.get("files", []) if x.get("extension") in {"md", "markdown"}]
            add_index(files, {"files": selected}, {"md": mode, "markdown": mode}, assets_bucket)
        else:
            selected = [x for x in payload.get("files", []) if x.get("extension") == extension]
            add_index(files, {"files": selected}, {extension: mode}, assets_bucket)
    for extension in ("html", "htm", "mht", "mhtml"):
        try:
            payload = read_json(fs, assets_bucket, f"documents/web/{extension}/index.json")
        except FileNotFoundError:
            continue
        add_index(files, payload, {extension: "h"}, assets_bucket)
    for extension in ("doc", "docx", "odt", "rtf"):
        payload = read_json(fs, assets_bucket, f"documents/office/{extension}/index.json")
        add_index(files, payload, {extension: "d" if extension in {"doc", "docx"} else "h"}, assets_bucket)
    for extension in ("xls", "xlsx", "csv", "ods"):
        try:
            payload = read_json(fs, assets_bucket, f"documents/spreadsheet/{extension}/index.json")
        except FileNotFoundError:
            continue
        add_index(files, payload, {extension: "h"}, assets_bucket)
    for extension in ("ppt", "pptx", "pps", "wps", "ps", "tif", "tiff"):
        try:
            payload = read_json(fs, assets_bucket, f"documents/pdf/{extension}/index.json")
        except FileNotFoundError:
            continue
        add_index(files, payload, {extension: "p"}, assets_bucket)
    for extension in ("epub", "mobi", "azw3", "fb2", "chm"):
        payload = read_json(fs, assets_bucket, f"chapters/ebook/{extension}/index.json")
        add_index(files, payload, {extension: "e"}, assets_bucket, chapters=True)
    old_image_payload = None
    try:
        old_image_payload = read_json(fs, assets_bucket, "pages/image/index.json")
    except FileNotFoundError:
        pass
    try:
        image_payload = read_json(fs, assets_bucket, "pages/image/index-v2.json")
    except FileNotFoundError:
        # Transitional only: the v2 index is built by a full image rebuild. Until
        # it exists, keep serving the old page-stream index so other format
        # publishes do not remove every image from the sidecar.
        if old_image_payload is None:
            raise
        image_payload = old_image_payload
    for entry in image_payload.get("files", []):
        if not isinstance(entry, dict) or not entry.get("key"):
            continue
        path = entry.get("path") if entry.get("mode") == "image" else None
        if not isinstance(path, str):
            path = entry.get("manifest")
        if isinstance(path, str):
            files[entry["key"]] = {"s": 2, "m": "i", "p": path, "b": assets_bucket}
    # Legacy TIF/TIFF PDF conversions are still indexed until the image rebuild
    # replaces every one of them with a single image or an image-page-stream.
    # Image entries are written above, so they always win for the same key.
    if old_image_payload is not None:
        for entry in old_image_payload.get("files", []):
            if not isinstance(entry, dict) or not entry.get("key") or entry["key"] in files:
                continue
            if str(entry.get("extension") or "").lower() not in {"tif", "tiff"}:
                continue
            manifest = entry.get("manifest")
            if isinstance(manifest, str):
                files[entry["key"]] = {"s": 2, "m": "i", "p": manifest, "b": assets_bucket}
    for kind, mode in (("audio", "a"), ("video", "v"), ("swf", "f")):
        payload = read_json(fs, assets_bucket, f"media/{kind}/index.json")
        for entry in payload.get("files", []):
            if not isinstance(entry, dict) or not entry.get("key"):
                continue
            object_path = entry.get("object")
            if isinstance(object_path, str):
                actual_mode = {"audio": "a", "video": "v", "swf": "f"}.get(entry.get("mode"), mode)
                files[entry["key"]] = {"s": 2, "m": actual_mode, "p": object_path, "b": assets_bucket}

    derived = read_json(fs, pdf_bucket, derived_path)
    for entry in derived.get("files", []):
        if not isinstance(entry, dict) or not entry.get("key") or not entry.get("new_path"):
            continue
        if entry["key"] not in files:
            files[entry["key"]] = {"s": 2, "m": "p", "p": entry["new_path"], "b": pdf_bucket}
    return {"v": 1, "f": dict(sorted(files.items()))}


def main() -> int:
    args = arguments()
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise RuntimeError("HF_TOKEN is required")
    fs = HfFileSystem(token=token)
    index = build(fs, args.assets_bucket, args.pdf_bucket, args.derived_manifest,
                  args.render_manifest, args.ocr_manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = gzip.compress(json.dumps(index, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode(), compresslevel=9, mtime=0)
    args.output.write_bytes(payload)
    print(f"mappings={len(index['f'])} bytes={len(payload)} output={args.output}")
    if args.apply:
        batch_bucket_files(args.assets_bucket, add=[(str(args.output), SIDECAR_PATH)], token=token)
        generation = publish_catalog(index, token)
        # Conversion workers upload immutable bundles under a processing root
        # before any index can reference them. The canonical sidecar/catalog
        # is the handoff boundary; only then may those roots be released.
        indexes = []
        for bucket, path in [(args.pdf_bucket, args.derived_manifest),
                             (args.assets_bucket, "documents/text/index.json"),
                             *stream_processing_indexes(args.assets_bucket),
                             *ebook_chapter_indexes(args.assets_bucket)]:
            try:
                payload = read_bucket_json(path, token, bucket=bucket)
            except (FileNotFoundError, OSError, ValueError):
                continue
            indexes.append(payload)
        released = release_processing_roots(
            indexes, generation, lambda path: read_bucket_json(path, token),
            lambda path, record: publish_json(path, record, token))
        print(f"published={args.assets_bucket}/{SIDECAR_PATH} catalog={generation} released_roots={released}")
    else:
        print("report-only; pass --apply to publish")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
