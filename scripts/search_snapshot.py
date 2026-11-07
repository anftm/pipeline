#!/usr/bin/env python3
"""Build and verify a local, content-addressed search-data snapshot.

This is deliberately not a publisher. The current daily sync and both live
sites remain untouched until a separate coordinated rollout exists.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
from pathlib import Path

SCHEMA = 1
KINDS = {'pages', 'space'}
SHAREABLE = ('search_data.json.gz', 'folder_tree.json.gz', 'folder_browser.json.gz')
SPACE_INDEXES = ('search_words.bin.gz', 'search_ngrams_2.bin.gz', 'search_ngrams_3.bin.gz')
MAX_DATA_GZIP = 32 * 1024 * 1024
MAX_DATA_EXPANDED = 256 * 1024 * 1024
MAX_OTHER_FILE = 16 * 1024 * 1024
MAX_FILES = 1024


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                       allow_nan=False) + '\n').encode('utf-8')


def safe_relative(value: str) -> str:
    if (not isinstance(value, str) or not value or value.startswith('/') or '\\' in value
            or any(part in {'', '.', '..'} for part in value.split('/'))
            or any(ord(c) < 32 for c in value)):
        raise ValueError('unsafe search snapshot path')
    return value


def list_files(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        raise ValueError('snapshot surface must be an ordinary directory')
    result = []
    for item in root.rglob('*'):
        if item.is_symlink():
            raise ValueError('snapshot cannot contain symlinks')
        if item.is_file():
            result.append(item)
            if len(result) > MAX_FILES:
                raise ValueError('search snapshot contains too many files')
        elif not item.is_dir():
            raise ValueError('snapshot contains a non-file object')
    return sorted(result)


def inspect_surface(root: Path, surface: str) -> tuple[dict, dict]:
    """Hash every local asset and validate the existing client file contract."""
    if surface not in KINDS:
        raise ValueError('unknown search surface')
    files = {}
    for path in list_files(root):
        name = safe_relative(path.relative_to(root).as_posix())
        limit = MAX_DATA_GZIP if name.endswith('.gz') else MAX_OTHER_FILE
        if path.stat().st_size > limit:
            raise ValueError('search snapshot member exceeds size limit: ' + name)
        raw = path.read_bytes()
        if not raw:
            raise ValueError('empty search snapshot member: ' + name)
        files[name] = {'sha256': sha(raw), 'bytes': len(raw)}
    mandatory = set(SHAREABLE) | ({*SPACE_INDEXES} if surface == 'space' else set())
    mandatory |= {'initial/manifest.json', 'initial/global.json', 'sidebar/manifest.json', 'sidebar/global.json'}
    allowed_exact = mandatory | set(SPACE_INDEXES)
    for name in files:
        if name not in allowed_exact and not re.fullmatch(r'(?:initial|sidebar)/repos/[^/]+\.json', name):
            raise ValueError('unexpected search snapshot member: ' + name)
    if not mandatory <= files.keys():
        raise ValueError('missing search snapshot member')
    if surface == 'pages' and set(SPACE_INDEXES) & files.keys():
        raise ValueError('Pages snapshot unexpectedly contains Space-only indexes')
    def unzip(name):
        try:
            with gzip.open(root / name, 'rb') as source:
                raw = source.read(MAX_DATA_EXPANDED + 1)
            if len(raw) > MAX_DATA_EXPANDED:
                raise ValueError('expanded search snapshot member exceeds size limit')
            return raw
        except (OSError, EOFError) as error:
            raise ValueError('invalid compressed search data: ' + name) from error
    values = {}
    for name in SHAREABLE:
        try:
            values[name] = json.loads(unzip(name))
        except (UnicodeDecodeError, ValueError) as error:
            raise ValueError('invalid compressed search data: ' + name) from error
    payload = values['search_data.json.gz']
    if any(not isinstance(values[name], dict) for name in SHAREABLE[1:]):
        raise ValueError('invalid search directory data')
    if (not isinstance(payload, dict) or payload.get('v') != 2 or not isinstance(payload.get('rp'), list)
            or not isinstance(payload.get('fd'), list) or not isinstance(payload.get('rc'), list)
            or not payload['rc'] or any(not isinstance(repo, str) for repo in payload['rp'])):
        raise ValueError('invalid compact search corpus')
    if any(not isinstance(folder, list) or any(not isinstance(part, str) for part in folder)
           for folder in payload['fd']):
        raise ValueError('invalid corpus folder dictionary')
    fields = hashlib.sha256()
    for row in payload['rc']:
        if (not isinstance(row, list) or len(row) != 6 or type(row[0]) is not int
                or not 0 <= row[0] < len(payload['rp']) or not isinstance(row[1], str)
                or not isinstance(row[2], str) or type(row[3]) is not int
                or not 0 <= row[3] < len(payload['fd']) or row[5] not in (0, 1)
                or type(row[5]) is not int):
            raise ValueError('invalid compact search record')
        for text in (row[1], payload['rp'][row[0]], '/'.join(payload['fd'][row[3]])):
            if not isinstance(text, str):
                raise ValueError('invalid corpus repository dictionary')
            fields.update(text.lower().encode('utf-8')); fields.update(b'\0')
    if surface == 'space':
        digest = fields.digest()
        for width in (2, 3):
            raw = unzip(f'search_ngrams_{width}.bin.gz')
            if not raw.startswith(b'VNG3' + bytes([width]) + digest):
                raise ValueError('search ngram index does not bind this corpus')
        raw = unzip('search_words.bin.gz')
        label = b'jieba-0.42.1-cjk-char-v1'
        if not raw.startswith(b'VWI2' + bytes([len(label)]) + label + digest):
            raise ValueError('search word index does not bind this corpus')
    del values, fields
    prefix = '/search/data/' if surface == 'pages' else '/data/'
    for family in ('initial', 'sidebar'):
        manifest = json.loads((root / family / 'manifest.json').read_bytes())
        urls = manifest.get('urls') if isinstance(manifest, dict) else None
        if not isinstance(manifest, dict) or manifest.get('version') != 2 or not isinstance(urls, list) or not urls:
            raise ValueError('invalid search bootstrap manifest')
        expected = set()
        for url in urls:
            if not isinstance(url, str) or not url.startswith(prefix):
                raise ValueError('search bootstrap URL belongs to another surface')
            name = safe_relative(url[len(prefix):])
            if not (name == family + '/global.json' or re.fullmatch(family + r'/repos/[^/]+\.json', name)):
                raise ValueError('invalid search bootstrap URL')
            if name not in files or name in expected:
                raise ValueError('missing or duplicate search bootstrap payload')
            expected.add(name)
            value = json.loads((root / name).read_bytes())
            if not isinstance(value, dict):
                raise ValueError('invalid search bootstrap payload')
        actual = {name for name in files if name == family + '/global.json' or name.startswith(family + '/repos/')}
        from urllib.parse import quote
        wanted = {family + '/global.json'} | {
            family + '/repos/' + quote(repo.split('/')[-1], safe='') + '.json' for repo in payload['rp']}
        if len(wanted) != len(payload['rp']) + 1 or expected != actual or expected != wanted:
            raise ValueError('missing or unlisted search bootstrap payload')
    return files, payload


def snapshot_identity(pages: Path, space: Path, revisions: dict, source: Path) -> dict:
    if not isinstance(revisions, dict) or not revisions:
        raise ValueError('source revisions must be a nonempty object')
    if any(not isinstance(name, str) or not re.fullmatch(r'VoiceOfML/[A-Za-z0-9._-]+', name)
           or not isinstance(rev, str) or not re.fullmatch(r'[0-9a-f]{40}', rev)
           for name, rev in revisions.items()):
        raise ValueError('invalid pinned dataset revision')
    page_files, page_payload = inspect_surface(pages, 'pages')
    space_files, space_payload = inspect_surface(space, 'space')
    if any(page_files[name] != space_files[name] for name in SHAREABLE) or page_payload != space_payload:
        raise ValueError('Pages and Space corpus assets do not match')
    if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_DATA_EXPANDED:
        raise ValueError('invalid captured source payload')
    source_bytes = source.read_bytes()
    if json.loads(source_bytes) != page_payload:
        raise ValueError('site corpus does not match the captured source payload')
    repos = page_payload['rp']
    if (any(not isinstance(repo, str) for repo in repos) or len(repos) != len(set(repos))
            or set(repos) != set(revisions)):
        raise ValueError('source revisions do not exactly cover corpus repositories')
    return {'version': SCHEMA, 'kind': 'search-snapshot-local', 'source_revisions': dict(sorted(revisions.items())),
            'capture_assurance': 'provided source bytes match corpus; provenance and live source heads not verified',
            'index_assurance': 'format header and corpus digest only; full postings semantics not verified',
            'source_payload': {'sha256': sha(source_bytes), 'bytes': len(source_bytes)},
            'record_count': len(page_payload['rc']), 'files': {'pages': page_files, 'space': space_files}}


def build_manifest(pages: Path, space: Path, revisions: dict, source: Path) -> dict:
    identity = snapshot_identity(pages, space, revisions, source)
    return {**identity, 'generation': sha(canonical(identity)), 'remote_writes': 0, 'publication_ready': False}


def verify_manifest(manifest: dict, pages: Path, space: Path, source: Path) -> dict:
    if (not isinstance(manifest, dict) or manifest.get('remote_writes') != 0
            or manifest.get('publication_ready') is not False):
        raise ValueError('invalid local search snapshot report')
    expected = build_manifest(pages, space, manifest.get('source_revisions'), source)
    if manifest != expected:
        raise ValueError('search snapshot bytes or identity changed')
    return expected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('build', 'verify'))
    parser.add_argument('--pages', type=Path, required=True, help='local Pages data directory')
    parser.add_argument('--space', type=Path, required=True, help='local Space data directory')
    parser.add_argument('--revisions', type=Path, help='captured source revisions (build only)')
    parser.add_argument('--source', type=Path, required=True, help='captured source search_data.json')
    parser.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == 'build':
        if args.revisions is None:
            parser.error('build requires --revisions')
        result = build_manifest(args.pages, args.space, json.loads(args.revisions.read_bytes()), args.source)
        output = args.manifest.resolve()
        if (output == args.source.resolve() or output == args.revisions.resolve()
                or any(output == root.resolve() or root.resolve() in output.parents for root in (args.pages, args.space))):
            raise ValueError('snapshot manifest output must be outside input assets')
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_bytes(canonical(result))
    else:
        result = verify_manifest(json.loads(args.manifest.read_bytes()), args.pages, args.space, args.source)
    print(json.dumps({'generation': result['generation'], 'record_count': result['record_count'],
                      'pages_files': len(result['files']['pages']), 'space_files': len(result['files']['space']),
                      'remote_writes': 0, 'publication_ready': False}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
