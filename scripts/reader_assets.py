#!/usr/bin/env python3
"""Shared contracts for incremental VoiceOfML reader assets."""

import json
import posixpath
import re
import urllib.parse
from pathlib import Path

MANIFEST_VERSION = 1
READER_ASSETS_REPO = "vomebook/Reader-Assets"
MANIFEST_NAME = "manifest.json"
CONVERTIBLE_EXTENSIONS = {
    "doc": ("libreoffice-docx-v2", "docx", "document.docx"),
    "docx": ("docx-native-v2", "docx", "document.docx"),
    "htm": ("sanitized-html-v3", "html", "document.html"),
    "html": ("sanitized-html-v3", "html", "document.html"),
    "mobi": ("calibre-epub-v2", "epub", "book.epub"),
    "azw3": ("calibre-epub-v2", "epub", "book.epub"),
    "fb2": ("calibre-epub-v2", "epub", "book.epub"),
    "odt": ("calibre-epub-v2", "epub", "book.epub"),
    "rtf": ("calibre-rtf-epub-v3", "epub", "book.epub"),
    "chm": ("calibre-chm-epub-v4", "epub", "book.epub"),
    "tif": ("pillow-pdf-v2", "pdf", "document.pdf"),
    "tiff": ("pillow-pdf-v2", "pdf", "document.pdf"),
    "djvu": ("djvulibre-pdf-v2", "pdf", "document.pdf"),
    "ppt": ("libreoffice-pdf-office-v2", "pdf", "document.pdf"),
    "pptx": ("libreoffice-pdf-office-v2", "pdf", "document.pdf"),
    "pps": ("libreoffice-pdf-office-v2", "pdf", "document.pdf"),
    "odp": ("libreoffice-pdf-office-v2", "pdf", "document.pdf"),
    "xls": ("libreoffice-pdf-office-v2", "pdf", "document.pdf"),
    "xlsx": ("libreoffice-pdf-office-xlsx-v3", "pdf", "document.pdf"),
    "csv": ("libreoffice-pdf-office-v2", "pdf", "document.pdf"),
    "ods": ("libreoffice-pdf-office-v2", "pdf", "document.pdf"),
    "wps": ("libreoffice-pdf-office-v2", "pdf", "document.pdf"),
    "mht": ("sanitized-mhtml-v4", "html", "document.html"),
    "mhtml": ("sanitized-mhtml-v4", "html", "document.html"),
    "ps": ("ghostscript-pdf-v4", "pdf", "document.pdf"),
    "caj": ("caj-family-pdf-v1", "pdf", "document.pdf"),
    "kdh": ("caj-family-pdf-v1", "pdf", "document.pdf"),
    "ape": ("ffmpeg-audio-mp3-v1", "audio", "audio.mp3"),
    "wma": ("ffmpeg-audio-mp3-v1", "audio", "audio.mp3"),
    "amr": ("ffmpeg-audio-mp3-v1", "audio", "audio.mp3"),
    "flv": ("ffmpeg-video-mp4-h264-aac-v1", "video", "video.mp4"),
    "f4v": ("ffmpeg-video-mp4-h264-aac-v1", "video", "video.mp4"),
    "rm": ("ffmpeg-video-mp4-h264-aac-v1", "video", "video.mp4"),
    "rmvb": ("ffmpeg-video-mp4-h264-aac-v1", "video", "video.mp4"),
    "mkv": ("ffmpeg-video-mp4-h264-aac-v1", "video", "video.mp4"),
    "avi": ("ffmpeg-video-mp4-h264-aac-v1", "video", "video.mp4"),
    "mpg": ("ffmpeg-video-mp4-h264-aac-v1", "video", "video.mp4"),
    "mpeg": ("ffmpeg-video-mp4-h264-aac-v1", "video", "video.mp4"),
    "mts": ("ffmpeg-video-mp4-h264-aac-v1", "video", "video.mp4"),
    "ts": ("ffmpeg-video-mp4-h264-aac-v1", "video", "video.mp4"),
    "wmv": ("ffmpeg-video-mp4-h264-aac-v1", "video", "video.mp4"),
}
PROTECTED_PDF_CONTRACT = ("qpdf-decrypted-v1", "pdf", "document.pdf")
PASSWORD_RE = re.compile(r"(?:密码|口令|password|passwd)\s*[：:=]?\s*([A-Za-z0-9]+)", re.IGNORECASE)
KNOWN_SOURCE_PASSWORDS = {
    ("VoiceOfML/MLMRL-Library", "基础入门书单/入门答疑/风正集.pdf"): "230505",
    ("VoiceOfML/MLMRL-Hub", "000269/1870520043_3072_风正集230220.pdf"): "230220",
    ("VoiceOfML/MLMRL-Hub", "001346/1870520043_15344_风正集230123.pdf"): "230123",
    ("VoiceOfML/MLMRL-Hub", "001346/1870520043_15348_风正集230505.pdf"): "230505",
}


def conversion_contract(extension: str, key: str = "") -> tuple[str, str, str]:
    return CONVERTIBLE_EXTENSIONS[extension]


def source_password(repo: str, path: str) -> str:
    match = PASSWORD_RE.search(path)
    if match:
        return match.group(1)
    return KNOWN_SOURCE_PASSWORDS.get((repo, path), "")


def source_conversion_contract(repo: str, path: str, extension: str):
    if extension in CONVERTIBLE_EXTENSIONS:
        return CONVERTIBLE_EXTENSIONS[extension]
    if extension == "pdf" and source_password(repo, path):
        return PROTECTED_PDF_CONTRACT
    return None


def empty_manifest() -> dict:
    return {"version": MANIFEST_VERSION, "files": {}}


def validate_manifest(manifest: dict) -> dict:
    if not isinstance(manifest, dict) or manifest.get("version") != MANIFEST_VERSION:
        raise ValueError("unsupported reader manifest version")
    if not isinstance(manifest.get("files"), dict):
        raise ValueError("reader manifest files must be an object")
    if "orphans" in manifest and not isinstance(manifest["orphans"], dict):
        raise ValueError("reader manifest orphans must be an object")
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
