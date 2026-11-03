#!/usr/bin/env python3
"""Explicit, resumable CHM corpus audit; never publishes or changes source data.

The snapshot contains production-records.json, manifest.json, jobs.json and
books/<source SHA>/book.{chm,epub}, fetched at the manifest's pinned revisions.
Browser checks compare every EPUB spine body's complete whitespace-normalized
text against both Reader normalizers. Source checks flag missing text fragments
and unmatched complete pages for review; they do not call approximate matches a
proof of complete conversion.
"""

import argparse
import collections
import email.policy
from email.parser import BytesParser
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import posixpath
import re
import subprocess
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile

from playwright.sync_api import sync_playwright

try:
    from .convert_reader_assets import decode_html_source
except ImportError:
    from convert_reader_assets import decode_html_source


def compact(text):
    return re.sub(r"\s+", "", text)


class SourceText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocked = []
        self.fragments = []
        self.scripts = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"head", "script", "style"}:
            self.blocked.append(tag)
        if tag == "script":
            self.scripts += 1

    def handle_endtag(self, tag):
        if tag in self.blocked:
            self.blocked = self.blocked[:self.blocked.index(tag)]

    def handle_data(self, data):
        if not self.blocked and (text := compact(data)):
            self.fragments.append(text)


def epub_documents(path):
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError("EPUB CRC mismatch")
        rootfile = ET.fromstring(archive.read("META-INF/container.xml")).find(".//{*}rootfile")
        opf = rootfile.attrib["full-path"]
        package = ET.fromstring(archive.read(opf))
        manifest = {item.attrib["id"]: item for item in package.findall(".//{*}manifest/{*}item")}
        docs, omitted = [], []
        for ref in package.findall(".//{*}spine/{*}itemref"):
            item = manifest[ref.attrib["idref"]]
            name = posixpath.normpath(posixpath.join(posixpath.dirname(opf), urllib.parse.unquote(item.attrib["href"].split("#")[0])))
            text = archive.read(name).decode("utf-8")
            root = ET.fromstring(text)
            body = root.find(".//{*}body")
            expected = compact("".join(body.itertext())) if body is not None else ""
            doc = {"name": name, "html": text, "expected": expected}
            if ref.get("linear") == "no":
                omitted.append({"name": name, "chars": len(expected)})
            else:
                docs.append(doc)
        if not docs:
            raise ValueError("EPUB has no readable spine")
        spine_names = {doc["name"] for doc in docs} | {doc["name"] for doc in omitted}
        unspined = []
        for name in archive.namelist():
            if Path(name).suffix.lower() in {".htm", ".html", ".xhtml"} and name not in spine_names:
                root = ET.fromstring(archive.read(name))
                body = root.find(".//{*}body")
                unspined.append({"name": name, "chars": len(compact("".join(body.itertext()))) if body is not None else 0})
        return docs, omitted, unspined


def html_documents(path):
    document = path.read_text(encoding="utf-8", errors="replace")
    parser = SourceText()
    parser.feed(document)
    expected = "".join(parser.fragments)
    return [{"name": path.name, "html": document, "expected": expected}], [], []


def source_documents(path):
    listing = subprocess.run(["7z", "l", "-slt", str(path)], capture_output=True, text=True, timeout=120)
    members = re.findall(r"^Path = (.+)$", listing.stdout, re.MULTILINE)[1:]
    sizes = [int(value) for value in re.findall(r"^Size = (\d+)\s*$", listing.stdout, re.MULTILINE)]
    if sum(sizes) > 2 * 1024**3 or len(members) > 10000:
        raise ValueError("CHM extraction exceeds audit limits")
    for member in members:
        normalized = member.replace("\\", "/")
        if normalized.startswith("/") or ".." in normalized.split("/"):
            raise ValueError("unsafe CHM member path")
    docs = []
    with tempfile.TemporaryDirectory(prefix="chm-audit-") as temporary:
        root = Path(temporary)
        result = subprocess.run(["7z", "x", "-y", f"-o{root}", str(path)], capture_output=True, timeout=120)
        for source in sorted(root.rglob("*")):
            if not source.is_file() or source.is_symlink():
                continue
            suffix = source.suffix.lower()
            if suffix in {".htm", ".html", ".xhtml", ".txt"}:
                text = decode_html_source(source)
                if suffix == ".txt":
                    docs.append((source.relative_to(root).as_posix(), [compact(text)], 0))
                    continue
                parser = SourceText()
                parser.feed(text)
                docs.append((source.relative_to(root).as_posix(), parser.fragments, parser.scripts))
            elif suffix in {".mht", ".mhtml"}:
                message = BytesParser(policy=email.policy.default).parsebytes(source.read_bytes())
                for index, part in enumerate(message.walk()):
                    if part.get_content_type() != "text/html":
                        continue
                    payload = part.get_payload(decode=True) or b""
                    try:
                        text = payload.decode(part.get_content_charset() or "utf-8")
                    except (LookupError, UnicodeDecodeError):
                        text = payload.decode("gb18030", "replace")
                    parser = SourceText()
                    parser.feed(text)
                    docs.append((f"{source.relative_to(root)}#{index}", parser.fragments, parser.scripts))
        return docs, {"listing_exit": listing.returncode, "extraction_exit": result.returncode,
                      "extraction_error": result.stderr.decode("utf-8", "replace")[:2000]}


BROWSER_CHECK = r"""({mode, documents}) => documents.map(item => {
  const expected = item.expected;
  const render = normalizer => {
    let raw = new DOMParser().parseFromString(item.html, 'text/html');
    if (mode === 'epub' && normalizer === window.auditAfter && /<[A-Za-z_][\w.-]*:html\b/.test(item.html)) {
      const xml = new DOMParser().parseFromString(item.html, 'application/xhtml+xml');
      if (!xml.querySelector('parsererror')) raw = xml;
    }
    const doc = mode === 'epub' ? normalizer.sanitizeEpubDocument(raw) : raw;
    for (const node of doc.querySelectorAll('style,link,script')) node.remove();
    const body = mode === 'epub' ? normalizer.epubContentBody(doc) : doc.body || doc.documentElement;
    const rendered = document.createElement('div');
    if (body && normalizer === window.auditAfter && mode === 'epub')
      rendered.append(...[...body.childNodes].map(node => document.importNode(node, true)));
    else rendered.innerHTML = body ? body.innerHTML : '';
    return rendered.textContent.replace(/\s+/gu, '');
  };
  const before = render(window.auditBefore), after = render(window.auditAfter);
  const digest = text => { let i=0; while(i<text.length && text[i]===expected[i])i++;
    return {chars:Array.from(text).length, first_difference:i, actual:text.slice(Math.max(0,i-50),i+150), expected:expected.slice(Math.max(0,i-50),i+150)}; };
  return {name:item.name, expected_chars:Array.from(expected).length,
    before_equal:before===expected, after_equal:after===expected,
    ...(before!==expected ? {before:digest(before)} : {}), ...(after!==expected ? {after:digest(after)} : {})};
})"""


def reader_functions(text):
    return text[text.index("const EPUB_HTML_TAGS"):text.index("function disposeReader()")]


def audit_book(job, folder, browser_page):
    entry = job["entry"]
    # Download cache names are stable; actual format is specified by manifest.
    artifact_name = "book.epub"
    for name, digest in [(artifact_name, entry["sha256"]), ("book.chm", entry["source_sha256"])]:
        if hashlib.sha256((folder / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"{name} digest mismatch")
    artifact_path = folder / artifact_name
    if entry.get("reader_mode") == "epub":
        docs, omitted, unspined = epub_documents(artifact_path)
        artifact_mode = "epub"
    elif entry.get("reader_mode") == "html":
        docs, omitted, unspined = html_documents(artifact_path)
        artifact_mode = "html"
    else:
        raise ValueError(f"unsupported CHM audit reader mode: {entry.get('reader_mode')}")
    chapters = []
    for offset in range(0, len(docs), 24):
        chapters.extend(browser_page.evaluate(BROWSER_CHECK, {"mode": artifact_mode, "documents": docs[offset:offset + 24]}))
    previous_path = folder / "audit.json"
    previous = json.loads(previous_path.read_text()) if previous_path.exists() else {}
    if previous.get("source_sha256") == entry["source_sha256"] and previous.get("artifact") == entry["path"] and previous.get("source_pages"):
        source_pages, extraction = previous["source_pages"], previous["extraction"]
    else:
        source, extraction = source_documents(folder / "book.chm")
        book_text = "\n".join(doc["expected"] for doc in docs)
        source_pages = []
        for name, fragments, scripts in source:
            text = "".join(fragments)
            missing = [fragment for fragment in fragments if fragment not in book_text]
            source_pages.append({"name": name, "chars": len(text), "exact_page_present": text in book_text,
                                 "scripts": scripts, "missing_chars": sum(map(len, missing)),
                                 "missing_fragments": missing})
    return {"source_sha256": entry["source_sha256"], "keys": job["keys"], "artifact": entry["path"],
            "chapters": chapters, "omitted_spine": omitted, "unspined": unspined,
            "source_pages": source_pages, "extraction": extraction,
            "source_chars": sum(row["chars"] for row in source_pages),
            "epub_chars": sum(row["expected_chars"] for row in chapters)}


def summarize(root, jobs, fingerprint):
    totals = collections.Counter(records=sum(len(job["keys"]) for job in jobs), artifacts=len(jobs))
    rows = []
    for job in jobs:
        source_sha = job["entry"]["source_sha256"]
        path = root / "books" / source_sha / "audit.json"
        row = {"source_sha256": source_sha, "keys": job["keys"]}
        if not path.exists():
            row["status"] = "not_checked"
        else:
            result = json.loads(path.read_text())
            if result.get("fingerprint") != fingerprint:
                row["status"] = "stale_check"
            elif "error" in result:
                row.update(status="check_failed", error=result["error"])
            else:
                row.update(status="checked", chapters=len(result["chapters"]),
                    before_bad=sum(not ch["before_equal"] for ch in result["chapters"]),
                    after_bad=sum(not ch["after_equal"] for ch in result["chapters"]),
                    source_pages=len(result["source_pages"]),
                    source_unmatched_pages=sum(not page["exact_page_present"] for page in result["source_pages"]),
                    source_missing_chars=sum(page["missing_chars"] for page in result["source_pages"]),
                    extraction=result["extraction"])
                totals["chapters"] += row["chapters"]
                totals["before_bad_chapters"] += row["before_bad"]
                totals["after_bad_chapters"] += row["after_bad"]
                totals["before_bad_artifacts"] += bool(row["before_bad"])
                totals["after_bad_artifacts"] += bool(row["after_bad"])
                totals["before_bad_records"] += len(job["keys"]) if row["before_bad"] else 0
                totals["source_review_artifacts"] += bool(row["source_missing_chars"])
        totals[row["status"]] += 1
        rows.append(row)
    report = {"fingerprint": fingerprint, "totals": dict(totals), "books": rows}
    (root / "audit-summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return dict(totals)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--reader-root", required=True, type=Path)
    parser.add_argument("--watch", action="store_true", help="Wait for pending downloads; stop when every fetch.json exists")
    args = parser.parse_args()
    root = args.snapshot.resolve()
    jobs = json.loads((root / "jobs.json").read_text())
    after = (args.reader_root / "static/reader.js").read_text()
    before = subprocess.check_output(["git", "-c", f"safe.directory={args.reader_root}", "show", "HEAD:static/reader.js"], cwd=args.reader_root).decode()
    fingerprint = hashlib.sha256((reader_functions(before) + reader_functions(after) + Path(__file__).read_text()).encode()).hexdigest()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page()
        for name, text in [("auditBefore", before), ("auditAfter", after)]:
            page.add_script_tag(content=f"window.{name} = (() => {{\n{reader_functions(text)}\nreturn {{sanitizeEpubDocument,epubContentBody}};}})();")
        while True:
            remaining = 0
            progress = 0
            for job in jobs:
                folder = root / "books" / job["entry"]["source_sha256"]
                output = folder / "audit.json"
                if output.exists() and json.loads(output.read_text()).get("fingerprint") == fingerprint:
                    continue
                if not (folder / "fetch.json").exists():
                    remaining += 1
                    continue
                try:
                    result = audit_book(job, folder, page)
                except Exception as exc:
                    result = {"error": str(exc), "type": type(exc).__name__}
                result["fingerprint"] = fingerprint
                output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
                progress += 1
                print(json.dumps({"book": job["keys"][0], "error": result.get("error"),
                    "chapters": len(result.get("chapters", [])),
                    "before_bad": sum(not row["before_equal"] for row in result.get("chapters", [])),
                    "after_bad": sum(not row["after_equal"] for row in result.get("chapters", [])),
                    "source_missing": sum(row["missing_chars"] for row in result.get("source_pages", []))}, ensure_ascii=False), flush=True)
            print(json.dumps(summarize(root, jobs, fingerprint)), flush=True)
            if not remaining or not args.watch:
                break
            if not progress:
                time.sleep(5)
        browser.close()


if __name__ == "__main__":
    main()
