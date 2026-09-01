#!/usr/bin/env python3
"""Shared contracts for incremental VoiceOfML reader assets."""

import json
import hashlib
import posixpath
import re
import urllib.parse
from pathlib import Path

MANIFEST_VERSION = 1
CHAPTER_MANIFEST_VERSION = 1
READER_ASSETS_REPO = "vomebook/Reader-Assets"
MANIFEST_NAME = "manifest.json"
CONVERTIBLE_EXTENSIONS = {
    "doc": ("libreoffice-docx-v2", "docx", "document.docx"),
    "docx": ("docx-native-v2", "docx", "document.docx"),
    "epub": ("foliate-original-v1", "foliate", "document.epub"),
    "htm": ("sanitized-html-v5", "html", "document.html"),
    "html": ("sanitized-html-v5", "html", "document.html"),
    "mobi": ("foliate-original-v1", "foliate", "document.mobi"),
    "azw3": ("foliate-original-v1", "foliate", "document.azw3"),
    "fb2": ("foliate-original-v1", "foliate", "document.fb2"),
    "odt": ("calibre-odt-html-v1", "html", "document.html"),
    "rtf": ("calibre-rtf-html-v1", "html", "document.html"),
    "chm": ("calibre-chm-html-v10", "html", "document.html"),
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
    "mht": ("sanitized-mhtml-v6", "html", "document.html"),
    "mhtml": ("sanitized-mhtml-v6", "html", "document.html"),
    "ps": ("ghostscript-pdf-v5", "pdf", "document.pdf"),
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
PASSWORD_RE = re.compile(
    r"(?:密码|口令|password|passwd)\s*(?:[：:=]\s*|(?=[A-Za-z0-9]))"
    r"([^\s\]〕】）)},，；;]+)",
    re.IGNORECASE,
)
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
        value = match.group(1)
        suffix = Path(path).suffix
        return value[:-len(suffix)] if suffix and value.lower().endswith(suffix.lower()) else value
    return KNOWN_SOURCE_PASSWORDS.get((repo, path), "")


def reusable_object_key(source_sha256: str, profile: str, *, extension: str = "",
                        source_revision: str = "", key: str = "") -> str:
    identity = f"{source_sha256}\0{profile}"
    if extension in {"htm", "html"}:
        identity += f"\0{source_revision}\0{key}"
    return identity


def object_profile_path(profile: str, *, extension: str = "", source_revision: str = "", key: str = "") -> str:
    if extension not in {"htm", "html"}:
        return profile
    context = hashlib.sha256(f"{source_revision}\0{key}".encode("utf-8")).hexdigest()[:16]
    return f"{profile}-{context}"


def validate_object_path(path: str) -> str:
    if (not isinstance(path, str) or not path.startswith("objects/") or "\\" in path
            or path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/"))):
        raise ValueError("invalid reader asset object path")
    return path


def validate_chapter_manifest(manifest: dict) -> dict:
    if not isinstance(manifest, dict) or manifest.get("version") != CHAPTER_MANIFEST_VERSION:
        raise ValueError("unsupported EPUB chapter manifest version")
    chapters = manifest.get("chapters")
    if manifest.get("kind") != "epub-chapters" or not isinstance(chapters, list) or not chapters:
        raise ValueError("invalid EPUB chapter manifest")
    seen = set()
    for index, chapter in enumerate(chapters, 1):
        if not isinstance(chapter, dict) or chapter.get("index") != index:
            raise ValueError("EPUB chapters must be ordered")
        path = chapter.get("path")
        if (not isinstance(path, str) or path.startswith("/") or "\\" in path
                or any(part in {"", ".", ".."} for part in path.split("/"))
                or not path.startswith("chapters/") or path in seen):
            raise ValueError("invalid EPUB chapter path")
        seen.add(path)
        if not isinstance(chapter.get("title", ""), str) or not isinstance(chapter.get("bytes"), int) or chapter["bytes"] <= 0:
            raise ValueError("invalid EPUB chapter metadata")
        if not re.fullmatch(r"[0-9a-f]{64}", str(chapter.get("sha256", ""))):
            raise ValueError("invalid EPUB chapter digest")
    search_index = manifest.get("search_index")
    if search_index is not None:
        if not isinstance(search_index, dict):
            raise ValueError("invalid EPUB search index metadata")
        search_path = search_index.get("path")
        if (not isinstance(search_path, str) or search_path != "epub-search-index.json.gz"
                or search_index.get("bytes", 0) <= 0
                or not re.fullmatch(r"[0-9a-f]{64}", str(search_index.get("sha256", "")))):
            raise ValueError("invalid EPUB search index metadata")
    if manifest.get("fallback") is not None:
        validate_object_path(manifest["fallback"])
    return manifest


def source_conversion_contract(repo: str, path: str, extension: str, source_bytes: int = 0):
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
    for key, entry in manifest["files"].items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            raise ValueError("reader manifest file entries must be objects")
        status = entry.get("status", "ready")
        if status not in {"ready", "failed"}:
            raise ValueError("reader manifest file entry has invalid status")
        if status == "ready":
            validate_object_path(entry.get("path"))
            for field in ("chapter_manifest", "fallback_path"):
                if field in entry:
                    validate_object_path(entry[field])
            if "chapter_manifest" in entry and not entry["chapter_manifest"].endswith("/chapter-manifest.json"):
                raise ValueError("invalid chapter manifest path")
            if "chapter_manifest" in entry and entry.get("reader_mode") not in {"epub", "foliate", "pdf"}:
                raise ValueError("chapter manifest requires EPUB or PDF reader mode")
            if "reader_mode" in entry and entry.get("reader_mode") not in {"pdf", "epub", "foliate", "docx", "html", "audio", "video"}:
                raise ValueError("reader manifest ready entry has invalid reader mode")
            if "bytes" in entry and (not isinstance(entry.get("bytes"), int) or entry["bytes"] <= 0):
                raise ValueError("reader manifest ready entry has invalid byte count")
            if "sha256" in entry and not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("sha256", ""))):
                raise ValueError("reader manifest ready entry has invalid digest")
    for path, entry in manifest.get("orphans", {}).items():
        validate_object_path(path)
        if isinstance(entry, dict) and entry.get("path") not in {None, path}:
            raise ValueError("reader asset orphan path mismatch")
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
