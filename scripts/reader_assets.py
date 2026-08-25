#!/usr/bin/env python3
"""Shared contracts for incremental VoiceOfML reader assets."""

import json
import posixpath
import urllib.parse
from pathlib import Path

MANIFEST_VERSION = 1
READER_ASSETS_REPO = "vomebook/Reader-Assets"
MANIFEST_NAME = "manifest.json"
CONVERTIBLE_EXTENSIONS = {
    "doc": ("libreoffice-docx-v1", "docx", "document.docx"),
    "docx": ("docx-native-v1", "docx", "document.docx"),
    "mobi": ("calibre-epub-v1", "epub", "book.epub"),
    "azw3": ("calibre-epub-v1", "epub", "book.epub"),
    "tif": ("pillow-pdf-v1", "pdf", "document.pdf"),
    "tiff": ("pillow-pdf-v1", "pdf", "document.pdf"),
    "djvu": ("djvulibre-pdf-v1", "pdf", "document.pdf"),
}
def conversion_contract(extension: str, key: str = "") -> tuple[str, str, str]:
    return CONVERTIBLE_EXTENSIONS[extension]


def empty_manifest() -> dict:
    return {"version": MANIFEST_VERSION, "files": {}}


def validate_manifest(manifest: dict) -> dict:
    if not isinstance(manifest, dict) or manifest.get("version") != MANIFEST_VERSION:
        raise ValueError("unsupported reader manifest version")
    if not isinstance(manifest.get("files"), dict):
        raise ValueError("reader manifest files must be an object")
    return manifest


def decode_search_payload(data) -> list[dict]:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    repos, folders, records = data.get("rp", []), data.get("fd", []), []
    for item in data.get("rc", []):
        if not isinstance(item, list) or len(item) < 6:
            continue
        repo = repos[item[0]] if isinstance(item[0], int) and 0 <= item[0] < len(repos) else ""
        folder = folders[item[3]] if isinstance(item[3], int) and 0 <= item[3] < len(folders) else []
        records.append({"Repo": repo, "File": item[1], "Extension": item[2], "Folder": folder, "Size": item[4]})
    return records


def relative_path(record: dict) -> str:
    name, extension = str(record.get("File") or ""), str(record.get("Extension") or "")
    filename = f"{name}.{extension}" if extension else name
    folders = [str(part) for part in (record.get("Folder") or [])]
    return posixpath.join(*folders, filename) if folders else filename


def asset_key(repo: str, path: str) -> str:
    return f"{repo}\0{path}"


def source_url(repo: str, revision: str, path: str) -> str:
    return f"https://huggingface.co/datasets/{repo}/resolve/{revision}/{urllib.parse.quote(path, safe='/')}"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_json(data: dict, *, pretty: bool = False) -> bytes:
    if pretty:
        text = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    else:
        text = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text.encode("utf-8")
