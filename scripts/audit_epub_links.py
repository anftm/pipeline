#!/usr/bin/env python3
"""Audit local EPUB/chapter-bundle links; no network or publication side effects."""

import argparse
from collections import Counter
from html.parser import HTMLParser
import json
from pathlib import Path
import posixpath
import re
from urllib.parse import unquote, urlsplit
import zipfile
from lxml import etree
from bleach._vendor import html5lib


class DocumentLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors = set()
        self.links = []

    def handle_starttag(self, tag, attrs):
        tag = tag.rsplit(":", 1)[-1].lower()
        attributes = dict(attrs)
        for name in ("id", "xml:id", "name"):
            if attributes.get(name):
                self.anchors.add(attributes[name])
        if tag in {"a", "area"} and attributes.get("href"):
            self.links.append(attributes["href"])


def inspect_documents(documents, members):
    parsed = {}
    for path, text in documents.items():
        parser = DocumentLinks()
        parser.feed(text)
        parsed[path] = parser
    counts = Counter()
    problems = []
    for path, document in parsed.items():
        for href in document.links:
            try:
                url = urlsplit(href)
            except ValueError:
                problems.append({"source": path, "href": href, "reason": "invalid_url"})
                continue
            if url.scheme or url.netloc:
                counts["external"] += 1
                continue
            target = posixpath.normpath(posixpath.join(posixpath.dirname(path), unquote(url.path))) if url.path else path
            fragment = unquote(url.fragment)
            if target not in members:
                reason = "missing_document"
            elif target in parsed and fragment and fragment not in parsed[target].anchors:
                reason = "missing_anchor"
            else:
                counts["valid_internal" if target in parsed else "attachment"] += 1
                continue
            problems.append({"source": path, "href": href, "target": target,
                             "fragment": fragment, "reason": reason})
    counts.update(problem["reason"] for problem in problems)
    return {"documents": len(documents), "counts": dict(counts), "problems": problems}


def audit_epub(path):
    with zipfile.ZipFile(path) as archive:
        members = set(archive.namelist())
        documents = {name: archive.read(name).decode("utf-8", "replace") for name in members
                     if Path(name).suffix.lower() in {".html", ".htm", ".xhtml"}}
    return inspect_documents(documents, members)


def audit_bundle(root):
    root = Path(root)
    manifest = json.loads((root / "chapter-manifest.json").read_text())
    documents = {entry["path"]: (root / entry["path"]).read_text() for entry in manifest["chapters"]}
    members = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    return inspect_documents(documents, members)


def body_text(document):
    try:
        root = etree.fromstring(document.encode(), etree.XMLParser(resolve_entities="internal", no_network=True, huge_tree=True))
    except etree.XMLSyntaxError:
        # Match browser table recovery (including foster parenting), rather than
        # libxml's different ordering for malformed source HTML.
        root = html5lib.parse(document)
    local = lambda node: node.tag.rsplit("}", 1)[-1] if isinstance(node.tag, str) else ""
    bodies = [node for node in root.iter() if local(node) == "body"]
    root = bodies[0] if bodies else root
    for node in root.iter():
        if local(node) not in {"script", "style", "noscript"}:
            continue
        node.text = ""
        for child in list(node):
            node.remove(child)
    def visible_text(node):
        if isinstance(node.tag, str):
            yield node.text or ""
            for child in node:
                yield from visible_text(child)
                yield child.tail or ""
    return re.sub(r"\s+", "", "".join(visible_text(root)))


def compare_bundle_source(epub, bundle):
    bundle = Path(bundle)
    manifest = json.loads((bundle / "chapter-manifest.json").read_text())
    differences = []
    characters = 0
    with zipfile.ZipFile(epub) as archive:
        for chapter in manifest["chapters"]:
            original = body_text(archive.read(chapter["source_path"]).decode("utf-8", "replace"))
            generated = body_text((bundle / chapter["path"]).read_text())
            characters += len(original)
            if original != generated:
                differences.append({"source": chapter["source_path"], "chapter": chapter["path"],
                                    "source_chars": len(original), "generated_chars": len(generated)})
    return {"chapters": len(manifest["chapters"]), "source_chars": characters,
            "differences": differences}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit_bundle(args.source) if args.source.is_dir() else audit_epub(args.source)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({"documents": result["documents"], "counts": result["counts"]}))


if __name__ == "__main__":
    main()
