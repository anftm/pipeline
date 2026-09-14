#!/usr/bin/env python3
"""Build a sanitized, independently fetchable EPUB chapter bundle."""

import hashlib
import gzip
import html
import json
import posixpath
import re
import zipfile
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

MAX_CHAPTER_RESOURCES = 2000
MAX_CHAPTER_RESOURCE_BYTES = 512 * 1024 * 1024

try:
    from .convert_reader_assets import sanitize_css, sanitize_html, sanitize_xml_document
    from .reader_assets import canonical_json, validate_chapter_manifest
except ImportError:
    from convert_reader_assets import sanitize_css, sanitize_html, sanitize_xml_document
    from reader_assets import canonical_json, validate_chapter_manifest


def _zip_path(base: str, href: str) -> str:
    value = unquote(str(href or "").split("#", 1)[0])
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        raise ValueError("external EPUB resource")
    result = posixpath.normpath(posixpath.join(base, parsed.path))
    if result.startswith("../") or result == ".." or "\\" in result:
        raise ValueError("unsafe EPUB resource path")
    return result


def _safe_resource_path(path: str) -> str:
    def replace(match):
        value = match.group()
        return "_x00_" if value.lower() == "x00" else f"_x{ord(value):02x}_"

    return re.sub(r"x00|[\x00-\x1f\x7f]", replace, path, flags=re.IGNORECASE)


def _local_name(node) -> str:
    return node.tag.rsplit("}", 1)[-1].lower() if isinstance(node.tag, str) else ""


def _node_text(node) -> str:
    return re.sub(r"\s+", " ", " ".join(node.itertext())).strip()


def _toc_titles(archive: zipfile.ZipFile, opf_path: str, manifest: dict, names: set[str]) -> dict[str, str]:
    titles = {}
    base = posixpath.dirname(opf_path)
    for item in manifest.values():
        media_type = item.get("media-type", "").lower()
        if media_type not in {"application/x-dtbncx+xml", "application/xhtml+xml", "text/html"}:
            continue
        try:
            toc_path = _zip_path(base, item.get("href", ""))
        except ValueError:
            continue
        if toc_path not in names:
            continue
        try:
            root = ET.fromstring(archive.read(toc_path))
        except ET.ParseError:
            continue
        if media_type == "application/x-dtbncx+xml":
            for node in (node for node in root.iter() if _local_name(node) == "navpoint"):
                content = next((child for child in node.iter() if _local_name(child) == "content"), None)
                label = next((child for child in node.iter() if _local_name(child) == "text"), None)
                if content is None or label is None:
                    continue
                try:
                    target = _zip_path(posixpath.dirname(toc_path), content.attrib.get("src", ""))
                except ValueError:
                    continue
                title = _node_text(label)
                if title:
                    titles.setdefault(target, title)
            continue
        for nav in (node for node in root.iter() if _local_name(node) == "nav"):
            for anchor in (node for node in nav.iter() if _local_name(node) == "a"):
                try:
                    target = _zip_path(posixpath.dirname(toc_path), anchor.attrib.get("href", ""))
                except ValueError:
                    continue
                title = _node_text(anchor)
                if title:
                    titles.setdefault(target, title)
    return titles


def _can_share_resource(path: str) -> bool:
    return Path(path).suffix.lower() in {
        ".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp",
        ".eot", ".otf", ".ttf", ".woff", ".woff2",
    }


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"script", "style", "noscript"}:
            self.skip_depth += 1

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style", "noscript"} and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data):
        if not self.skip_depth:
            self.parts.append(data)


class _TitleExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.capture = False

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"title", "h1", "h2", "h3"} and not self.parts:
            self.capture = True

    def handle_endtag(self, tag):
        if tag.lower() in {"title", "h1", "h2", "h3"}:
            self.capture = False

    def handle_data(self, data):
        if self.capture:
            self.parts.append(data)


def _document_title(document: str) -> str:
    parser = _TitleExtractor()
    parser.feed(document)
    parser.close()
    return re.sub(r"\s+", " ", html.unescape(" ".join(parser.parts))).strip()


def _chapter_text(document: str) -> str:
    parser = _TextExtractor()
    parser.feed(document)
    parser.close()
    return re.sub(r"\s+", " ", html.unescape(" ".join(parser.parts))).strip()


def build_bundle(epub: Path, output: Path, *, fallback: str | None = None,
                 include_resources: bool = True) -> dict:
    """Write chapter files and return the validated manifest.

    The output directory contains only files intended for a dataset commit.
    """
    output.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(epub) as archive:
        names = set(archive.namelist())
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        rootfile = next((node for node in container.iter() if _local_name(node) == "rootfile"), None)
        if rootfile is None:
            raise ValueError("EPUB package is missing")
        opf_path = rootfile.attrib.get("full-path", "")
        opf = ET.fromstring(archive.read(opf_path))
        base = posixpath.dirname(opf_path)
        manifest = {}
        for node in opf.iter():
            if _local_name(node) == "item":
                manifest[node.attrib.get("id", "")] = node.attrib
        chapters = []
        search_chapters = []
        chapter_records = []
        resource_usage = Counter()
        toc_titles = _toc_titles(archive, opf_path, manifest, names)
        for number, ref in enumerate((n for n in opf.iter() if _local_name(n) == "itemref"), 1):
            item = manifest.get(ref.attrib.get("idref"))
            if (not item or "nav" in item.get("properties", "").split()
                    or item.get("media-type", "").lower() not in {"application/xhtml+xml", "text/html"}):
                continue
            source_path = _zip_path(base, item.get("href", ""))
            if source_path not in names:
                # Keep readable chapters when a broken package has one stale spine entry.
                continue
            document = archive.read(source_path).decode("utf-8", "replace")
            try:
                clean = sanitize_xml_document(document)
            except RuntimeError as exc:
                if str(exc) != "EPUB XML content is malformed":
                    raise
                # Some EPUBs label HTML as XHTML but contain recoverable HTML.
                clean = sanitize_html(document, allow_relative=True)
            chapter_index = len(chapter_records) + 1
            resources = set()
            def rewrite(match):
                value = match.group(2)
                try:
                    resource = _zip_path(posixpath.dirname(source_path), value)
                except ValueError:
                    return match.group(0)
                if resource not in names or resource.lower().endswith((".xhtml", ".html", ".htm")):
                    return match.group(0)
                resources.add(resource)
                safe_resource = _safe_resource_path(resource)
                return f'{match.group(1)}="../resources/__CHAPTER_RESOURCE__/{safe_resource}"'
            clean = re.sub(r'((?:src|href))=["\']([^"\'#]+)["\']', rewrite, clean, flags=re.I)
            title = toc_titles.get(source_path) or _document_title(clean) or f"章节 {number}"
            chapter_records.append({"index": chapter_index, "title": title, "clean": clean, "resources": resources})
            resource_usage.update(resources)
        if not chapter_records:
            raise ValueError("EPUB spine has no readable chapters")
        for record in chapter_records:
            chapter_index = record["index"]
            clean = record["clean"]
            resources = record["resources"]
            if include_resources:
                resource_bytes = sum(archive.getinfo(resource).file_size for resource in resources)
                if (len(resources) > MAX_CHAPTER_RESOURCES
                        or resource_bytes > MAX_CHAPTER_RESOURCE_BYTES):
                    raise ValueError("EPUB chapter resource budget exceeded")
                for resource in sorted(resources):
                    prefix = "shared" if resource_usage[resource] > 1 and _can_share_resource(resource) else f"chapter-{chapter_index:04d}"
                    target = output / "resources" / prefix / _safe_resource_path(resource)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    data = archive.read(resource)
                    if resource.lower().endswith(".css"):
                        data = sanitize_css(data.decode("utf-8", "replace")).encode("utf-8")
                    if not target.exists():
                        target.write_bytes(data)
                    clean = clean.replace(
                        f"../resources/__CHAPTER_RESOURCE__/{_safe_resource_path(resource)}",
                        f"../resources/{prefix}/{_safe_resource_path(resource)}",
                    )
            target = output / "chapters" / f"chapter-{chapter_index:04d}.xhtml"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(clean, encoding="utf-8")
            data = target.read_bytes()
            chapters.append({"index": chapter_index, "title": record["title"], "path": target.relative_to(output).as_posix(), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            search_chapters.append({"index": chapter_index, "title": record["title"], "path": target.relative_to(output).as_posix(), "text": _chapter_text(clean)})
    search_data = canonical_json({"version": 1, "kind": "epub-search-index", "chapters": search_chapters})
    search_bytes = gzip.compress(search_data, mtime=0)
    search_target = output / "epub-search-index.json.gz"
    search_target.write_bytes(search_bytes)
    result = {"version": 1, "kind": "epub-chapters", "chapters": chapters, "search_index": {"path": search_target.relative_to(output).as_posix(), "bytes": len(search_bytes), "sha256": hashlib.sha256(search_bytes).hexdigest()}}
    if fallback:
        result["fallback"] = fallback
    validate_chapter_manifest(result)
    (output / "chapter-manifest.json").write_bytes(canonical_json(result, pretty=True))
    return result
