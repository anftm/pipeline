#!/usr/bin/env python3
"""Build a sanitized, independently fetchable EPUB chapter bundle."""

import hashlib
import json
import posixpath
import re
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

try:
    from .convert_reader_assets import sanitize_css, sanitize_xml_document
    from .reader_assets import canonical_json, validate_chapter_manifest
except ImportError:
    from convert_reader_assets import sanitize_css, sanitize_xml_document
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


def build_bundle(epub: Path, output: Path, *, fallback: str | None = None) -> dict:
    """Write chapter files and return the validated manifest.

    The output directory contains only files intended for a dataset commit.
    """
    output.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(epub) as archive:
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        rootfile = next((node for node in container.iter() if node.tag.rsplit("}", 1)[-1] == "rootfile"), None)
        if rootfile is None:
            raise ValueError("EPUB package is missing")
        opf_path = rootfile.attrib.get("full-path", "")
        opf = ET.fromstring(archive.read(opf_path))
        base = posixpath.dirname(opf_path)
        manifest = {}
        for node in opf.iter():
            if node.tag.rsplit("}", 1)[-1] == "item":
                manifest[node.attrib.get("id", "")] = node.attrib
        chapters = []
        resources = set()
        for number, ref in enumerate((n for n in opf.iter() if n.tag.rsplit("}", 1)[-1] == "itemref"), 1):
            item = manifest.get(ref.attrib.get("idref"))
            if not item or item.get("media-type", "").lower() not in {"application/xhtml+xml", "text/html"}:
                continue
            source_path = _zip_path(base, item.get("href", ""))
            if source_path not in archive.namelist():
                raise ValueError("EPUB spine resource is missing")
            clean = sanitize_xml_document(archive.read(source_path).decode("utf-8", "replace"))
            def rewrite(match):
                value = match.group(2)
                try:
                    resource = _zip_path(posixpath.dirname(source_path), value)
                except ValueError:
                    return match.group(0)
                if resource not in archive.namelist() or resource.lower().endswith((".xhtml", ".html", ".htm")):
                    return match.group(0)
                return f'{match.group(1)}="../resources/{resource}"'
            clean = re.sub(r'((?:src|href))=["\']([^"\'#]+)["\']', rewrite, clean, flags=re.I)
            target = output / "chapters" / f"chapter-{len(chapters) + 1:04d}.xhtml"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(clean, encoding="utf-8")
            data = target.read_bytes()
            chapters.append({"index": len(chapters) + 1, "title": f"章节 {number}", "path": target.relative_to(output).as_posix(), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            for match in re.finditer(r"(?:src|href)=[\"']\.\./resources/([^\"'#]+)", clean, re.I):
                resources.add(posixpath.normpath(match.group(1)))
        if not chapters:
            raise ValueError("EPUB spine has no readable chapters")
        for resource in sorted(resources):
            target = output / "resources" / resource
            target.parent.mkdir(parents=True, exist_ok=True)
            data = archive.read(resource)
            if resource.lower().endswith(".css"):
                data = sanitize_css(data.decode("utf-8", "replace")).encode("utf-8")
            target.write_bytes(data)
    result = {"version": 1, "kind": "epub-chapters", "chapters": chapters}
    if fallback:
        result["fallback"] = fallback
    validate_chapter_manifest(result)
    (output / "chapter-manifest.json").write_bytes(canonical_json(result, pretty=True))
    return result
