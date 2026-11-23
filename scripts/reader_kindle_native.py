"""Experimental Kindle ingress that preserves native NCX identity through EPUB."""
from __future__ import annotations

from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
import posixpath
import subprocess
import sys
import zipfile

from scripts import epub_chapters as epub
from scripts.reader_chapter_profiles import KINDLE_PROFILE as CANDIDATE_PROFILE


def _navigation(archive):
    names = set(archive.namelist())
    container = epub._parse_package_xml(archive.read('META-INF/container.xml'))
    package, = (node.get('full-path') for node in container.iter() if epub._local_name(node) == 'rootfile')
    root = epub._parse_package_xml(archive.read(package))
    items = {n.get('id'): n.attrib for n in root.iter() if epub._local_name(n) == 'item'}
    spine = next(n for n in root.iter() if epub._local_name(n) == 'spine')
    base = posixpath.dirname(package)
    docs = {epub._zip_path(base, items[n.get('idref')]['href']) for n in spine if epub._local_name(n) == 'itemref'}
    ncx = items.get(spine.get('toc'))
    if not ncx:
        raise ValueError('native NCX unavailable; cannot certify this alternate conversion')
    entries = epub._toc_entries(archive, package, {ncx['id']: ncx}, names, docs, ncx['id'])
    if not entries:
        raise ValueError('native NCX empty or unreadable')
    return entries, docs


def _document_anchors(document):
    root = epub.ET.fromstring(document)
    body = next((node for node in root.iter() if epub._local_name(node) == 'body'), None)
    if body is None:
        raise ValueError('candidate chapter has no body')
    return Counter(value for node in body.iter() for value in {node.get('id'), node.get('name')} if value)


def _raw_html_anchors(document):
    # KindleUnpack's MOBI7 handoff is HTML, not necessarily XML. Do not
    # serialize or modify it just to count the anchors Calibre will consume.
    class Anchors(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.counts = Counter()
        def handle_starttag(self, _tag, attrs):
            self.counts.update({value for key, value in attrs if key in {'id', 'name'} and value})
    parser = Anchors()
    parser.feed(document)
    parser.close()
    return parser.counts


def verify_output(source_entries, target, *, exact_paths=False):
    """Require every native stop to survive with the same label, depth and anchor.

    Filenames may change when Calibre splits chapters. An unanchored target or
    ambiguous ID is not enough evidence to certify a cross-document move. A
    native page-start target can remain unanchored if no split took place.
    """
    with zipfile.ZipFile(target) as archive:
        rows, docs = _navigation(archive)
        if len(rows) != len(source_entries):
            raise ValueError('native directory entry count changed during EPUB generation')
        anchors, verified = {}, []
        for row, original in zip(rows, source_entries, strict=True):
            path, fragment = row['source_path'], original['fragment']
            if ((row['title'], row['depth']) != (original['title'], original['depth'])
                    or (exact_paths and (path, row['fragment']) != (original['source_path'], fragment))
                    or (not exact_paths and row['fragment'] not in ('', fragment))):
                raise ValueError('native directory identity changed during EPUB generation')
            if path not in docs or not (fragment or exact_paths):
                raise ValueError('native destination lacks a provable XHTML anchor')
            if path not in anchors:
                contents = archive.read(path).decode('utf-8')
                anchors[path] = _document_anchors(epub.sanitize_xml_document(contents))
            if fragment and anchors[path][fragment] != 1:
                raise ValueError('native destination missing or ambiguous after cleanup')
            # Calibre elides some NCX fragments in the first split document.
            # Restore only the exact original marker on the file it selected;
            # never infer a replacement from a label or a different document.
            verified.append({**row, 'fragment': fragment})
        positions = {(row['source_path'], row['fragment']) for row in verified}
        source_positions = {(row['source_path'], row['fragment']) for row in source_entries}
        if not exact_paths and (len(positions) != len(source_positions)
                               or len({f for _, f in source_positions}) != len(source_positions)
                               or len({f for _, f in positions}) != len(positions)):
            raise ValueError('ambiguous cross-document native anchor identity')
    return verified


def _opf_navigation(package):
    root = epub._parse_package_xml(package.read_bytes())
    items = {n.get('id'): n.attrib for n in root.iter() if epub._local_name(n) == 'item'}
    spine = next(n for n in root.iter() if epub._local_name(n) == 'spine')
    ncx = items.get(spine.get('toc'))
    if not ncx:
        raise ValueError('native NCX unavailable')
    # Use the same local-target resolver as packaged EPUB; read only this OPF's
    # designated NCX. All path arguments remain relative to the unpacked root.
    name = epub._zip_path('', ncx['href'])
    docs = {epub._zip_path('', items[n.get('idref')]['href']) for n in spine if epub._local_name(n) == 'itemref'}
    class Navigation:
        @staticmethod
        def read(path):
            if path != name:
                raise ValueError('unexpected native NCX member')
            return (package.parent / name).read_bytes()
    rows = epub._toc_entries(Navigation(), package.name, {ncx['id']: ncx}, {name}, docs, ncx['id'])
    if not rows:
        raise ValueError('native NCX empty or unreadable')
    # KindleUnpack is run externally at an explicitly selected revision. Do
    # not accept numeric positions unless it actually materialized the anchor.
    anchors = {}
    for row in rows:
        path, fragment = row['source_path'], row['fragment']
        if path not in docs or not fragment:
            raise ValueError('native NCX target is not an anchored spine document')
        if path not in anchors:
            data = (package.parent / path).read_text(encoding='utf-8')
            anchors[path] = _raw_html_anchors(data)
        if anchors[path][fragment] != 1:
            raise ValueError('native NCX references an absent or duplicate source anchor')
    return rows


def convert(source, work, unpacker):
    """Try an isolated experiment; no fallback may masquerade as native output."""
    unpacker = Path(unpacker).resolve(strict=True)
    unpacked = work/'native-kindle'
    subprocess.run([sys.executable, str(unpacker), str(source.resolve()), str(unpacked.resolve())],
                   check=True, timeout=600, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    kf8_candidates = list((unpacked/'mobi8').glob('*.epub'))
    if len(kf8_candidates) > 1:
        raise ValueError('multiple preferred Kindle packages')
    if kf8_candidates:
        kf8 = kf8_candidates[0]
        with zipfile.ZipFile(kf8) as archive:
            native, _ = _navigation(archive)
        return kf8, verify_output(native, kf8, exact_paths=True)
    package = unpacked/'mobi7'/'content.opf'
    if source.suffix.lower() == '.azw3' or not package.is_file():
        raise ValueError('missing preferred Kindle variant')
    native = _opf_navigation(package)
    target = work/'native-kindle.epub'
    subprocess.run(['ebook-convert', str(package), str(target), '--epub-version', '2', '--no-default-epub-cover',
                    '--flow-size', '0'], check=True, timeout=600,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return target, verify_output(native, target)


def verify_bundle(native, bundle, manifest):
    toc = manifest.get('toc', [])
    signature = lambda r: (r['title'], r['depth'], r['fragment'])
    if [signature(r) for r in toc] != [signature(r) for r in native]:
        raise ValueError('native directory identity did not survive chapter bundling')
    anchors = {}
    for row, expected in zip(toc, native, strict=True):
        chapter = manifest['chapters'][row['chapter']-1]
        if chapter['source_path'] != expected['source_path']:
            raise ValueError('candidate chapter bundle mapped native target to another document')
        path = chapter['path']
        if path not in anchors:
            anchors[path] = _document_anchors((bundle/path).read_text(encoding='utf-8'))
        if row['fragment'] and anchors[path][row['fragment']] != 1:
            raise ValueError('candidate chapter bundle lost native destination')
