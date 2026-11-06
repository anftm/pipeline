#!/usr/bin/env python3
"""Preview one Reader route withdrawal. This command never writes or deletes.

The plan compares only direct sidecar references. It is not a complete reference
analysis, does not update source/search indexes, and cannot authorize deletion.
"""
from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import io
import json
import re
import sys

try:
    from . import build_reader_assets_v2_sidecar as source_indexes
    from . import publish_reader_v3 as publication, reader_lifecycle as lifecycle, shared
    from .reader_bucket_store import HubBucketStore
except ImportError:
    import build_reader_assets_v2_sidecar as source_indexes
    import publish_reader_v3 as publication
    import reader_lifecycle as lifecycle
    import shared
    from reader_bucket_store import HubBucketStore


SIDECAR = 'reader-index/reader_assets.json.gz'
MAX_EXPANDED = 50 * 1024 * 1024

# Exact current inputs read by build_reader_assets_v2_sidecar.build(). This list
# is used only for a bounded, read-only preview, never as deletion authority.
INDEXES = tuple(dict.fromkeys([
    (publication.ASSETS, 'documents/text/index.json', True),
    *((publication.ASSETS, f'documents/web/{ext}/index.json', False)
      for ext in ('html', 'htm', 'mht', 'mhtml')),
    *((publication.ASSETS, f'documents/office/{ext}/index.json', True)
      for ext in ('doc', 'docx', 'odt', 'rtf')),
    *((publication.ASSETS, f'documents/spreadsheet/{ext}/index.json', False)
      for ext in ('xls', 'xlsx', 'csv', 'ods')),
    *((publication.ASSETS, f'documents/pdf/{ext}/index.json', False)
      for ext in ('ppt', 'pptx', 'pps', 'wps', 'ps', 'tif', 'tiff')),
    *((publication.ASSETS, path, True) for _, path in source_indexes.ebook_chapter_indexes(publication.ASSETS)),
    (publication.ASSETS, 'pages/image/index.json', False),
    (publication.ASSETS, 'pages/image/index-v2.json', False),
    *((publication.ASSETS, f'media/{kind}/index.json', True) for kind in ('audio', 'video', 'swf')),
    (shared.PDF_PAGES_BUCKET, 'reader-index/derived_pdf_manifest.json', True),
]))


def index_kind(path: str) -> str:
    if path == 'documents/text/index.json':
        return 'text-stream-index'
    if path.startswith('documents/web/'):
        return 'web-document-stream-index'
    if path.startswith('documents/office/'):
        return 'office-document-stream-index'
    if path.startswith('documents/spreadsheet/'):
        return 'spreadsheet-html-stream-index'
    if path.startswith('documents/pdf/'):
        return 'static-pdf-stream-index'
    if path.startswith('chapters/ebook/'):
        return 'ebook-chapter-stream-index'
    if path.startswith('pages/image/'):
        return 'image-stream-index'
    if path.startswith('media/'):
        return 'media-stream-index'
    if path == 'reader-index/derived_pdf_manifest.json':
        return 'pdf-derived-source-index'
    raise ValueError('unknown source index')


def source_key(repo: str, path: str) -> str:
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*', repo)
            or not path or path.startswith('/') or '\\' in path
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or any(part in {'', '.', '..'} for part in path.split('/'))):
        raise ValueError('source must be an exact repository and file path')
    return repo + '\0' + path


def decode_sidecar(raw: bytes) -> dict:
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
        expanded = stream.read(MAX_EXPANDED + 1)
    if len(expanded) > MAX_EXPANDED:
        raise ValueError('canonical sidecar expands beyond limit')
    payload = json.loads(expanded)
    if not isinstance(payload, dict) or payload.get('v') != 1 or not isinstance(payload.get('f'), dict):
        raise ValueError('invalid canonical sidecar')
    return payload


def refs(entry) -> set[tuple[str, str]]:
    if entry is None:
        return set()
    if not isinstance(entry, dict):
        raise ValueError('invalid sidecar entry')
    return {(ref['bucket'], ref['path']) for ref in lifecycle.sidecar_references({'f': {'entry': entry}})}


def inspect_source_indexes(store, key: str) -> dict:
    """Find the exact source row in known builders' inputs, without any listings."""
    sources, errors, seen_image = [], [], set()
    for bucket, path, required in INDEXES:
        try:
            raw = store.read_bytes(bucket, path)
        except FileNotFoundError:
            if required:
                errors.append({'bucket': bucket, 'path': path, 'error': 'missing'})
            continue
        except Exception as error:
            errors.append({'bucket': bucket, 'path': path, 'error': type(error).__name__})
            continue
        try:
            payload = json.loads(raw)
            if (not isinstance(payload, dict) or payload.get('version') != 1
                    or payload.get('kind') != index_kind(path) or not isinstance(payload.get('files'), list)):
                raise ValueError('invalid source index')
            if path in {'pages/image/index.json', 'pages/image/index-v2.json'}:
                seen_image.add(path)
            matches = []
            for row in payload['files']:
                if not isinstance(row, dict) or not isinstance(row.get('key'), str):
                    raise ValueError('invalid source index row')
                if row['key'] == key:
                    matches.append(row)
            if matches:
                sources.append({'bucket': bucket, 'path': path, 'sha256': hashlib.sha256(raw).hexdigest(),
                                'entries': matches})
        except (ValueError, TypeError, UnicodeError):
            errors.append({'bucket': bucket, 'path': path, 'error': 'invalid-index'})
    if not seen_image:
        errors.append({'bucket': publication.ASSETS, 'path': 'pages/image/index*.json', 'error': 'missing'})
    return {'complete': not errors, 'matched_indexes': sources, 'errors': errors}


def preview(store, repo: str, path: str, *, scan_sources=False) -> dict:
    """Read one consistent-enough snapshot and simulate removing its projected route."""
    key = source_key(repo, path)
    raw = store.read_bytes(publication.ASSETS, SIDECAR)
    canonical = decode_sidecar(raw)
    pointer, catalog = publication.current(store)
    base = publication.pinned_base(store, catalog, canonical) if pointer else canonical
    projection = publication.project_sidecar(base, catalog) if pointer else copy.deepcopy(canonical)
    layers = {'canonical': canonical['f'], 'pinned_base': base['f'], 'expected_projection': projection['f']}
    selected = {name: entries.get(key) for name, entries in layers.items()}
    targets = set().union(*(refs(entry) for entry in selected.values()))
    owners: dict[tuple[str, str], dict[str, set[str]]] = {target: {} for target in targets}
    for layer, entries in layers.items():
        for owner, entry in entries.items():
            if owner == key:
                continue
            for ref in refs(entry) & targets:
                owners[ref].setdefault(layer, set()).add(owner)
    remaining = copy.deepcopy(projection)
    remaining['f'].pop(key, None)
    source_report = inspect_source_indexes(store, key) if scan_sources else {
        'complete': False, 'matched_indexes': [], 'errors': [], 'reason': 'not-scanned'}
    # Recheck mutable roots so an ordinary overlapping publication is detected.
    # This does not create an atomic snapshot or prevent later writers.
    if store.read_bytes(publication.ASSETS, SIDECAR) != raw or publication.current(store)[0] != pointer:
        raise ValueError('Reader indexes changed during preview; retry')
    suppressed = catalog.get('suppressed_sources', [])
    return {
        'version': 1,
        'kind': 'reader-route-withdrawal-preview',
        'source': {'repo': repo, 'path': path, 'key': key},
        'canonical_sha256': hashlib.sha256(raw).hexdigest(),
        'v3_generation': pointer['generation'] if pointer else None,
        'pinned_base_sha256': (catalog.get('base_sidecar') or {}).get('sha256'),
        'canonical_differs_from_pinned': bool(catalog.get('base_sidecar') and
                                               catalog['base_sidecar']['sha256'] != hashlib.sha256(raw).hexdigest()),
        'routes': selected,
        'v3_override': catalog['files'].get(key),
        'suppressed': key in suppressed,
        'source_indexes': source_report,
        'direct_resources': [
            {'bucket': bucket, 'path': resource_path,
             'other_direct_owners': {layer: sorted(keys) for layer, keys in sorted(owners[(bucket, resource_path)].items())}}
            for bucket, resource_path in sorted(targets)
        ],
        'simulated_projection': {
            'key_removed': key in projection['f'],
            'before_count': len(projection['f']),
            'after_count': len(remaining['f']),
            'sha256': hashlib.sha256(publication.encode(remaining)).hexdigest(),
        },
        'warnings': [
            'Preview only: no index, source, search record or object was changed.',
            'Expected projection is computed locally; deployed sites are not contacted or verified.',
            'Direct sidecar references only; nested manifests, other roots, caches and external users are not checked.',
            'Ordinary unify preserves declared suppressions; unknown or historical writers may not.',
            'Source index matches and completeness are point-in-time hints, not an atomic snapshot.',
            'This report cannot establish that an object is unreferenced or authorize deletion.',
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True, help='exact source dataset namespace/name')
    parser.add_argument('--path', required=True, help='exact source file path')
    parser.add_argument('--scan-sources', action='store_true',
                        help='also read the known current-format source indexes (no bucket listing)')
    args = parser.parse_args(argv)
    try:
        result = preview(HubBucketStore(), args.repo, args.path, scan_sources=args.scan_sources)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except Exception as error:
        print(f'{type(error).__name__}: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
