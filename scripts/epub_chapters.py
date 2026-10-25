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
SEARCH_TEXT_PROFILE = "dom-text-v1"

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


def bundle_version(output: Path) -> str:
    """Include chapters and every resource in an immutable bundle identity."""
    digest = hashlib.sha256()
    for path in sorted(file for file in output.rglob('*') if file.is_file()):
        digest.update(path.relative_to(output).as_posix().encode('utf-8') + b'\0')
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()[:16]


def _safe_resource_path(path: str) -> str:
    def replace(match):
        value = match.group()
        return "_x00_" if value.lower() == "x00" else f"_x{ord(value):02x}_"

    return re.sub(r"x00|[\x00-\x1f\x7f]", replace, path, flags=re.IGNORECASE)


def _local_name(node) -> str:
    return node.tag.rsplit("}", 1)[-1].lower() if isinstance(node.tag, str) else ""


def _parse_package_xml(raw: bytes):
    """Parse package XML and repair undeclared prefixes used by old EPUBs."""
    raw = re.sub(rb"\s+xmlns:xmlns\s*=\s*(['\"])[^'\"]*\1", b"", raw,
                 flags=re.IGNORECASE)
    try:
        return ET.fromstring(raw)
    except ET.ParseError as error:
        if "unbound prefix" not in str(error):
            raise
        text = raw.decode("utf-8", "replace")
        match = re.search(r"<([A-Za-z_][\w.-]*)(?:\s[^>]*)?>", text, re.S)
        if not match:
            raise
        root_tag = match.group(1)
        declared = set(re.findall(r"xmlns:([A-Za-z_][\w.-]*)\s*=", match.group(0)))
        used = set(re.findall(r"(?<!xmlns:)([A-Za-z_][\w.-]*):[A-Za-z_][\w.-]*", text))
        # Namespace declaration attributes also match the prefix scanner as
        # ``xmlns:name``. The reserved ``xmlns`` prefix must never be added.
        missing = sorted(prefix for prefix in used
                         if prefix not in declared and prefix not in {root_tag, "xmlns"})
        if not missing:
            raise
        replacement = match.group(0)[:-1] + "".join(
            f' xmlns:{prefix}="urn:reader-repair:{prefix}"' for prefix in missing
        ) + ">"
        repaired = text[:match.start()] + replacement + text[match.end():]
        return ET.fromstring(repaired.encode("utf-8"))


def _node_text(node) -> str:
    return re.sub(r"\s+", " ", " ".join(node.itertext())).strip()


def _toc_entries(archive: zipfile.ZipFile, opf_path: str, manifest: dict, names: set[str]) -> list[dict]:
    """Read the navigation tree, independently of physical spine splits."""
    base = posixpath.dirname(opf_path)
    candidates = sorted(manifest.values(), key=lambda item: "nav" not in item.get("properties", "").split())
    for item in candidates:
        media_type = item.get("media-type", "").lower()
        if media_type != "application/x-dtbncx+xml" and "nav" not in item.get("properties", "").split():
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
        entries = []
        def add(label, href, depth):
            if not href:
                return
            try:
                target = _zip_path(posixpath.dirname(toc_path), href)
            except ValueError:
                # Broken generated books occasionally contain TOC targets that
                # escape the EPUB root. Do not let one unusable navigation entry
                # reject the readable spine or expose a path outside the bundle.
                return
            entries.append({"title": label, "source_path": target,
                            "fragment": unquote(urlsplit(href).fragment), "depth": depth})
        def ncx(parent, depth=0):
            for node in parent:
                if _local_name(node) != "navpoint":
                    continue
                content = next((child for child in node if _local_name(child) == "content"), None)
                label = next((child for child in node if _local_name(child) == "navlabel"), None)
                if content is not None:
                    add(_node_text(label) if label is not None else "", content.get("src"), depth)
                ncx(node, depth + 1)
        def nav_list(parent, depth=0):
            for li in parent:
                if _local_name(li) != "li":
                    continue
                anchor = next((child for child in li if _local_name(child) in {"a", "span"}), None)
                nested = [child for child in li if _local_name(child) == "ol"]
                if anchor is not None:
                    href = anchor.get("href")
                    # An unlinked group shares its first child's destination.
                    if not href:
                        first = next((child for ol in nested for child in ol.iter()
                                      if _local_name(child) == "a" and child.get("href")), None)
                        href = first.get("href") if first is not None else None
                    add(_node_text(anchor), href, depth)
                for ol in nested:
                    nav_list(ol, depth + 1)
        if media_type == "application/x-dtbncx+xml":
            for node in root.iter():
                if _local_name(node) == "navmap":
                    ncx(node)
        else:
            for nav in root.iter():
                kinds = nav.get("{http://www.idpf.org/2007/ops}type", "").split()
                if _local_name(nav) == "nav" and ("toc" in kinds or nav.get("role") == "doc-toc"):
                    for ol in nav:
                        if _local_name(ol) == "ol":
                            nav_list(ol)
        if entries:
            return entries
    return []


def _placeholder_title(title: str) -> bool:
    return not title.strip() or title.strip().lower() in {"unknown text", "untitled", "unknown", "无标题"}


def _target_title(document: str, fragment: str = "", *, root=None) -> str:
    if root is None:
        try:
            root = ET.fromstring(document)
        except ET.ParseError:
            return _document_title(document)
    body = next((node for node in root.iter() if _local_name(node) == "body"), root)
    nodes = list(body.iter())
    start = 0
    if fragment:
        start = next((i for i, node in enumerate(nodes)
                      if node.get("id") == fragment or node.get("name") == fragment), -1)
        if start < 0:
            return ""
        text = _node_text(nodes[start])
        if text and len(text) <= 500 and not _placeholder_title(text):
            return text
    for node in nodes[start:]:
        if _local_name(node) in {"h1", "h2", "h3", "h4", "h5", "h6", "p"}:
            text = _node_text(node)
            if text and len(text) <= 500 and not _placeholder_title(text):
                return text
    if not fragment:
        title = next((_node_text(node) for node in root.iter() if _local_name(node) == "title"), "")
        if not _placeholder_title(title):
            return title
    return ""


COVER_TITLES = {"start", "cover", "titlepage", "title page", "封面", "扉页", "目录", "总目录"}


def _cover_like(record: dict) -> bool:
    title = str(record.get("title") or "").strip().lower()
    return (record.get("source_path", "").lower().endswith(("titlepage.xhtml", "cover.xhtml"))
            or title in COVER_TITLES)


def _fallback_toc(records: list[dict]) -> list[dict]:
    """A TOC whose only entry is the cover sends readers to a blank page."""
    toc = []
    for record in records:
        if _cover_like(record):
            continue
        title = str(record.get("title") or "").strip()
        if _placeholder_title(title):
            title = f"章节 {record['index']}"
        toc.append({"title": title, "chapter": record["index"], "fragment": "", "depth": 0})
    return toc


def bundle_toc(entries: list[dict], records: list[dict]) -> list[dict]:
    by_source = {record["source_path"]: record for record in reversed(records)}
    toc = []
    documents = {}
    for entry in entries:
        record = by_source.get(entry["source_path"])
        if record is None:
            # Broken packages often put cover/nav pages in the TOC without
            # putting them in the readable spine. Preserve the actual spine
            # and omit only the unusable navigation item.
            continue
        if entry["source_path"] not in documents:
            try:
                documents[entry["source_path"]] = ET.fromstring(record["clean"])
            except ET.ParseError:
                documents[entry["source_path"]] = None
        title = entry["title"]
        if _placeholder_title(title):
            title = _target_title(record["clean"], entry["fragment"], root=documents[entry["source_path"]])
        if _placeholder_title(title):
            document_title = _document_title(record["clean"])
            title = (document_title if document_title and not _placeholder_title(document_title)
                     else f"章节 {record['index']}")
        fragment = entry["fragment"]
        root = documents.get(entry["source_path"])
        if fragment and root is not None and not any(
                node.get("id") == fragment or node.get("name") == fragment
                for node in root.iter()):
            # Keep a usable chapter destination when the source TOC points at
            # an anchor omitted by the source document or its sanitizer.
            fragment = ""
        if root is not None:
            fragment = usable_toc_fragment(root, fragment, title)
        toc.append({"title": title, "chapter": record["index"],
                    "fragment": fragment, "depth": entry["depth"]})
    if all(_cover_like(next((r for r in records if r["index"] == item["chapter"]), {}))
           for item in toc):
        # Some packages declare only a cover/start page in their navigation.
        # Readable chapters exist, so rebuild navigation from their titles
        # rather than sending every reader to a blank cover.
        rebuilt = _fallback_toc(records)
        if rebuilt:
            return rebuilt
    return toc


def _normalized_label(value: str) -> str:
    return re.sub(r"[\s\u00a0]+", "", str(value or "")).casefold()


def _heading_node(root, fragment: str, title: str):
    """Find the heading a navigation label names, ignoring the source anchor.

    Some source packages point several navigation entries at the same in-page
    footnote anchor (duokan "note1"/"footnote1"), or at an anchor whose text is not
    the label. Trusting that anchor sends readers to the wrong poem or section, so a
    label that matches a heading in this document wins over a shared/generic anchor.
    """
    label = _normalized_label(title)
    if len(label) < 2:
        return None
    candidates = []
    for node in root.iter():
        if _local_name(node) not in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "div"}:
            continue
        text = _normalized_label(_node_text(node))
        if not text or len(text) > 120:
            continue
        if text == label:
            candidates.append((0, node))
        elif text.startswith(label) or label.startswith(text[: len(label)]):
            candidates.append((1, node))
        elif label in text:
            candidates.append((2, node))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _chapter_headings(clean: str) -> list[str]:
    """Normalized heading labels of one sanitized chapter, in document order.

    Sanitized MOBI/EPUB documents keep the `html:` prefix and often put the visible title in
    the `title` attribute of an empty heading element, so both shapes have to be read.
    """
    labels = []
    # Self-closing headings (<h1 title="..." />) are common in converted books, so match the
    # opening tag and treat the element as empty unless a body follows before the next tag.
    for match in re.finditer(r"<(?:html:)?h([1-6])\b([^>]*?)(?:/>|>(.*?)</(?:html:)?h\1>)",
                             clean, re.I | re.S):
        text = _normalized_label(re.sub(r"<[^>]+>", "", match.group(3) or ""))
        if not text:
            attribute = re.search(r'\btitle\s*=\s*"([^"]*)"', match.group(2))
            text = _normalized_label(attribute.group(1)) if attribute else ""
        if text:
            labels.append(text)
    return labels


def _label_matches(label: str, headings: list[str]) -> bool:
    if not label:
        return False
    for heading in headings:
        if heading == label or heading.startswith(label) or label.startswith(heading):
            return True
    return False


def relocate_toc_entries(toc_entries: list[dict], records: list[dict]) -> list[dict]:
    """Point navigation entries at the chapter that actually contains their label.

    Some source packages carry a navigation anchor that names a footnote and lands in the
    wrong physical document (duokan MOBI conversions do this for every entry), so a label
    that is absent from its assigned chapter is looked up in the chapters that readably own
    that heading. Entries whose label cannot be found keep their source destination.
    """
    headings = {}
    for record in records:
        headings[record["index"]] = _chapter_headings(record.get("clean") or "")
    located = []
    for entry in toc_entries:
        label = _normalized_label(entry["title"])
        record = next((item for item in records if item["source_path"] == entry["source_path"]), None)
        if record is not None and _label_matches(label, headings.get(record["index"], [])):
            located.append(dict(entry))
            continue
        match = next((item for item in records
                      if _label_matches(label, headings.get(item["index"], []))), None)
        if match is None:
            located.append(dict(entry))
            continue
        relocated = dict(entry)
        relocated["source_path"] = match["source_path"]
        relocated["fragment"] = ""
        located.append(relocated)
    return located


def usable_toc_fragment(root, fragment: str, title: str) -> str:
    """Return the anchor the reader should land on for this navigation label.

    A label that names a heading in this document is always preferred: sharing an anchor
    between entries (or pointing at a footnote/generic anchor) is what makes a directory
    jump land on the wrong verse. Without a matching heading the source anchor stays, and a
    missing anchor falls back to the chapter start.
    """
    node = _heading_node(root, fragment, title)
    if node is None:
        if not fragment:
            return ""
        return fragment if any(node.get("id") == fragment or node.get("name") == fragment
                               for node in root.iter()) else ""
    for key in ("id", "name"):
        value = str(node.get(key) or "").strip()
        if value:
            return value
    # A heading without an id cannot be targeted; keep the source anchor only if it exists.
    return fragment if any(node.get("id") == fragment or node.get("name") == fragment
                           for node in root.iter()) else ""


def link_inline_toc(clean: str, record: dict, chapter_paths: dict[str, str],
                    records: list[dict], toc_entries: list[dict]) -> str:
    """Link plain paragraph TOCs found in EPUBs that omitted hrefs entirely."""
    targets = {
        re.sub(r"\s+", " ", str(item.get("title") or "")).strip(): chapter_paths[item["source_path"]]
        for item in records
        if item.get("source_path") in chapter_paths and item.get("title")
    }
    targets.update({
        re.sub(r"\s+", " ", str(item.get("title") or "")).strip(): chapter_paths[item["source_path"]]
        for item in toc_entries
        if item.get("source_path") in chapter_paths and item.get("title")
    })
    if (len(targets) < 2 or re.search(r"<a\b", clean, re.I)
            or not ("目录" in record.get("title", "")
                    or sum(clean.count(html.escape(title)) for title in targets) >= 2)):
        return clean
    paragraph = re.compile(r"(<(?:html:)?p\b[^>]*>)(\s*)([^<]+?)(\s*)(</(?:html:)?p>)", re.I)
    def replace(match):
        title = re.sub(r"\s+", " ", match.group(3)).strip()
        target = targets.get(title)
        if not target:
            return match.group(0)
        return (f'{match.group(1)}{match.group(2)}<a href="{html.escape(target, quote=True)}">'
                f'{match.group(3)}</a>{match.group(4)}{match.group(5)}')
    return paragraph.sub(replace, clean)


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
        if tag.rsplit(":", 1)[-1].lower() in {"script", "style", "noscript"}:
            self.skip_depth += 1

    def handle_endtag(self, tag):
        if tag.rsplit(":", 1)[-1].lower() in {"script", "style", "noscript"} and self.skip_depth:
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
        if tag.rsplit(":", 1)[-1].lower() in {"title", "h1", "h2", "h3"} and not self.parts:
            self.capture = True

    def handle_endtag(self, tag):
        if tag.rsplit(":", 1)[-1].lower() in {"title", "h1", "h2", "h3"}:
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
    # HTMLParser already decoded markup references once. A second unescape
    # would turn displayed literals such as '&nbsp;' or '&#0' into other text.
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def write_search_index(output: Path, chapters: list[dict]) -> dict:
    """Index exact verified chapter bytes; mark the DOM mapping convention."""
    search_chapters = []
    for chapter in chapters:
        data = (output / chapter["path"]).read_bytes()
        if len(data) != chapter["bytes"] or hashlib.sha256(data).hexdigest() != chapter["sha256"]:
            raise ValueError("chapter bytes differ before search indexing")
        search_chapters.append({"index": chapter["index"], "title": chapter["title"],
                                "path": chapter["path"], "text": _chapter_text(data.decode("utf-8"))})
    search_data = canonical_json({"version": 1, "kind": "epub-search-index",
                                 "text_profile": SEARCH_TEXT_PROFILE, "chapters": search_chapters})
    search_bytes = gzip.compress(search_data, mtime=0)
    target = output / "epub-search-index.json.gz"
    target.write_bytes(search_bytes)
    return {"path": target.name, "bytes": len(search_bytes),
            "sha256": hashlib.sha256(search_bytes).hexdigest(), "text_profile": SEARCH_TEXT_PROFILE}


def build_bundle(epub: Path, output: Path, *, fallback: str | None = None,
                 include_resources: bool = True, include_all_documents: bool = False) -> dict:
    """Write chapter files and return the validated manifest.

    The output directory contains only files intended for a dataset commit.
    """
    output.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(epub) as archive:
        names = set(archive.namelist())
        container = _parse_package_xml(archive.read("META-INF/container.xml"))
        rootfile = next((node for node in container.iter() if _local_name(node) == "rootfile"), None)
        if rootfile is None:
            raise ValueError("EPUB package is missing")
        opf_path = rootfile.attrib.get("full-path", "")
        opf = _parse_package_xml(archive.read(opf_path))
        base = posixpath.dirname(opf_path)
        manifest = {}
        for node in opf.iter():
            if _local_name(node) == "item":
                manifest[node.attrib.get("id", "")] = node.attrib
        chapters = []
        chapter_records = []
        resource_usage = Counter()
        toc_entries = _toc_entries(archive, opf_path, manifest, names)
        toc_titles = {}
        for entry in toc_entries:
            if not _placeholder_title(entry["title"]):
                toc_titles.setdefault(entry["source_path"], entry["title"])
        document_items = {}
        for item in manifest.values():
            if ("nav" in item.get("properties", "").split()
                    or item.get("media-type", "").lower() not in {"application/xhtml+xml", "text/html"}):
                continue
            try:
                item_path = _zip_path(base, item.get("href", ""))
            except ValueError:
                # An out-of-root manifest item cannot be a valid EPUB chapter.
                continue
            document_items[item_path] = item
        spine_paths = []
        for ref in opf.iter():
            if _local_name(ref) != "itemref":
                continue
            item = manifest.get(ref.attrib.get("idref"))
            if not item or "nav" in item.get("properties", "").split():
                continue
            try:
                candidate = _zip_path(base, item.get("href", ""))
            except ValueError:
                continue
            if candidate in document_items:
                spine_paths.append(candidate)
        document_paths = spine_paths + ([path for path in sorted(document_items) if path not in spine_paths]
                                         if include_all_documents else [])
        for number, source_path in enumerate(document_paths, 1):
            item = document_items[source_path]
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
                except ValueError as error:
                    if str(error) == "unsafe EPUB resource path":
                        return ""
                    return match.group(0)
                if resource not in names or resource.lower().endswith((".xhtml", ".html", ".htm")):
                    return match.group(0)
                resources.add(resource)
                safe_resource = _safe_resource_path(resource)
                return f'{match.group(1)}="../resources/__CHAPTER_RESOURCE__/{safe_resource}"'
            clean = re.sub(r'((?:src|href))=["\']([^"\'#]+)["\']', rewrite, clean, flags=re.I)
            title = toc_titles.get(source_path) or _target_title(clean) or _document_title(clean) or f"章节 {number}"
            chapter_records.append({"index": chapter_index, "source_path": source_path,
                                    "title": title, "clean": clean, "resources": resources})
            resource_usage.update(resources)
        if not chapter_records:
            raise ValueError("EPUB spine has no readable chapters")
        toc = bundle_toc(relocate_toc_entries(toc_entries, chapter_records), chapter_records)
        # Resolve links only after every readable spine item has its final name.
        # Original filenames cannot be used after chapters move into the bundle.
        chapter_paths = {}
        for record in chapter_records:
            chapter_paths.setdefault(record["source_path"], f'chapter-{record["index"]:04d}.xhtml')
        for record in chapter_records:
            chapter_index = record["index"]
            clean = record["clean"]
            def rewrite_chapter_link(match):
                value = html.unescape(match.group(2))
                if value.startswith("#"):
                    return match.group(0)
                try:
                    original = _zip_path(posixpath.dirname(record["source_path"]), value)
                    fragment = urlsplit(value).fragment
                except ValueError as error:
                    if str(error) == "unsafe EPUB resource path":
                        return ""
                    return match.group(0)
                target = chapter_paths.get(original)
                if target is None:
                    return match.group(0)
                if fragment:
                    target += "#" + fragment
                return f'{match.group(1)}="{html.escape(target, quote=True)}"'
            clean = re.sub(r'(?<![\w:-])(href)\s*=\s*["\']([^"\']*)["\']',
                           rewrite_chapter_link, clean, flags=re.I)
            clean = link_inline_toc(clean, record, chapter_paths, chapter_records, toc_entries)
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
            chapters.append({"index": chapter_index, "title": record["title"], "source_path": record["source_path"], "path": target.relative_to(output).as_posix(), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    result = {"version": 1, "kind": "epub-chapters", "chapters": chapters,
              "search_index": write_search_index(output, chapters)}
    result["toc"] = toc
    if fallback:
        result["fallback"] = fallback
    validate_chapter_manifest(result)
    (output / "chapter-manifest.json").write_bytes(canonical_json(result, pretty=True))
    return result
