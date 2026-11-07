#!/usr/bin/env python3
"""Read-only inventory and exact legacy-PDF migration plan for pdf-pages-v2.

No output from this command is deletion authority. In particular, inventories
are not atomic and runtime references may exist outside this bucket.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys

from huggingface_hub import HfApi

try:
    from . import shared
    from .reader_bucket_store import HubBucketStore
except ImportError:
    import shared
    from reader_bucket_store import HubBucketStore


BUCKET = shared.PDF_PAGES_BUCKET
ASSETS = shared.READER_ASSETS_BUCKET
INDEX = 'reader-index/derived_pdf_manifest.json'
SIDECAR = 'reader-index/reader_assets.json.gz'
POINTER = 'reader-index/v3/current.json'
OLD = re.compile(r'derived/[A-Za-z0-9._-]+/[a-f0-9]{32}/document\.pdf\Z')
NEW = re.compile(r'objects/([a-f0-9]{2})/([a-f0-9]{64})/([a-f0-9]{16})/document\.pdf\Z')
SHA = re.compile(r'[a-f0-9]{64}\Z')
FIELDS = ('key', 'repo', 'source_path', 'source_extension', 'profile', 'source_revision',
          'source_sha256', 'derived_sha256', 'derived_bytes', 'old_path', 'source_uri', 'generation', 'bucket')


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def decode_sidecar(raw):
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
        body = stream.read(50 * 1024 * 1024 + 1)
    if len(body) > 50 * 1024 * 1024:
        raise ValueError('sidecar expands beyond limit')
    value = json.loads(body)
    if not isinstance(value, dict) or value.get('v') != 1 or not isinstance(value.get('f'), dict):
        raise ValueError('invalid Reader sidecar')
    return value


def summarize(files):
    counts, sizes = Counter(), Counter()
    for path, entry in files.items():
        name = path.rsplit('/', 1)[-1]
        category = ('document.pdf' if name == 'document.pdf' else 'document.djvu' if name == 'document.djvu'
                    else 'pages/*' if '/pages/' in path else 'preview/*' if '/preview/' in path
                    else 'text/*' if '/text/' in path else 'ocr/*' if '/ocr/' in path else name)
        counts[category] += 1
        sizes[category] += entry['size']
    return {'files': sum(counts.values()), 'bytes': sum(sizes.values()),
            'by_kind': {name: {'files': counts[name], 'bytes': sizes[name]} for name in sorted(counts)}}


def validate_entry(entry):
    if not isinstance(entry, dict) or not OLD.fullmatch(str(entry.get('new_path', ''))):
        raise ValueError('invalid legacy derived entry')
    key, sha, content = entry.get('key'), entry.get('source_sha256'), entry.get('derived_sha256')
    if (not isinstance(key, str) or key.count('\0') != 1 or not all(key.split('\0'))
            or not isinstance(sha, str) or not SHA.fullmatch(sha)
            or not isinstance(content, str) or not SHA.fullmatch(content)
            or type(entry.get('derived_bytes')) is not int or entry['derived_bytes'] < 1
            or entry.get('bucket') != BUCKET
            or entry.get('manifest_path') != entry['new_path'].rsplit('/', 1)[0] + '/derived-manifest.json'):
        raise ValueError('invalid legacy derived identity')


def object_manifest_matches(entry, candidate, manifest):
    if not isinstance(manifest, dict) or manifest.get('version') != 1 or manifest.get('kind') != 'pdf-derived-source':
        return False
    if any(manifest.get(field) != entry.get(field) for field in FIELDS):
        return False
    return (manifest.get('new_path') == candidate and
            manifest.get('manifest_path') == candidate.rsplit('/', 1)[0] + '/derived-manifest.json')


def build_plan(index, derived, objects, read_manifest, *, sidecar=None, active_redirects=None):
    """Map each indexed legacy entry only to an exact same-key/content copy."""
    if (not isinstance(index, dict) or index.get('version') != 1 or index.get('kind') != 'pdf-derived-source-index'
            or not isinstance(index.get('files'), list)):
        raise ValueError('invalid canonical derived index')
    entries = index['files']
    if any(not isinstance(item, dict) or not isinstance(item.get('key'), str) for item in entries):
        raise ValueError('invalid canonical index entries')
    if len({item['key'] for item in entries}) != len(entries):
        raise ValueError('duplicate canonical source keys')
    by_content = defaultdict(list)
    for path, item in objects.items():
        match = NEW.fullmatch(path)
        if match and match.group(1) == match.group(2)[:2]:
            by_content[(match.group(2), item['xet_hash'], item['size'])].append(path)
    active_redirects = active_redirects or {}
    if active_redirects:
        try:
            from .reader_page_redirects import validated
        except ImportError:
            from reader_page_redirects import validated
        validated({'derived_pdf_redirects': active_redirects})
    old_by_new = {new: old for old, new in active_redirects.items()}
    cache = {}
    rows, unresolved = [], []
    for entry in entries:
        current_path = entry.get('new_path', '')
        reconciled = isinstance(current_path, str) and current_path in old_by_new
        if reconciled:
            old_path = old_by_new[current_path]
            old_manifest = old_path.rsplit('/', 1)[0] + '/derived-manifest.json'
            legacy_entry = {**entry, 'new_path': old_path, 'manifest_path': old_manifest}
            validate_entry(legacy_entry)
            if entry.get('manifest_path') != current_path.rsplit('/', 1)[0] + '/derived-manifest.json':
                raise ValueError('reconciled PDF manifest path differs')
            if entry.get('path') == old_path:
                raise ValueError('reconciled PDF index still uses an old path')
        else:
            old_path = current_path
            if not isinstance(old_path, str) or not old_path.startswith('derived/'):
                continue
            validate_entry(entry)
            old_manifest = entry['manifest_path']
        old = derived.get(old_path)
        if old is None or old['size'] != entry['derived_bytes'] or not old.get('xet_hash'):
            unresolved.append({'key': entry['key'], 'old_path': old_path, 'reason': 'legacy-object-missing-or-mismatched'})
            continue
        candidates = ([current_path] if reconciled and current_path in objects
                      and objects[current_path]['xet_hash'] == old['xet_hash']
                      and objects[current_path]['size'] == old['size'] else [] if reconciled
                      else by_content.get((entry['source_sha256'], old['xet_hash'], old['size']), []))
        matches, errors = [], []
        for path in sorted(candidates):
            manifest_path = path.rsplit('/', 1)[0] + '/derived-manifest.json'
            meta = objects.get(manifest_path)
            if not meta:
                errors.append({'path': manifest_path, 'error': 'missing-in-inventory'})
                continue
            try:
                if manifest_path not in cache:
                    cache[manifest_path] = read_manifest(manifest_path)
                raw = cache[manifest_path]
                if len(raw) != meta['size']:
                    raise ValueError('manifest size changed')
                value = json.loads(raw)
                if object_manifest_matches(entry, path, value):
                    matches.append((path, manifest_path, hashlib.sha256(raw).hexdigest()))
            except Exception as error:
                errors.append({'path': manifest_path, 'error': type(error).__name__})
        if errors or len(matches) != 1:
            unresolved.append({'key': entry['key'], 'old_path': old_path,
                               'reason': 'manifest-unreadable' if errors else 'no-unique-matching-manifest',
                               'candidate_count': len(candidates), 'matching_manifests': len(matches), 'errors': errors})
            continue
        path, manifest_path, manifest_sha = matches[0]
        route = sidecar.get('f', {}).get(entry['key']) if sidecar is not None else None
        rows.append({'key': entry['key'], 'source_sha256': entry['source_sha256'],
                     'derived_sha256': entry['derived_sha256'], 'bytes': old['size'],
                     'old_path': old_path, 'old_manifest': old_manifest, 'old_xet_hash': old['xet_hash'],
                     'new_path': path, 'new_manifest': manifest_path, 'new_manifest_sha256': manifest_sha,
                     'index_location': 'objects' if reconciled else 'derived',
                     'currently_direct_route': bool(isinstance(route, dict) and route.get('p') == old_path
                                                    and route.get('b', ASSETS) == BUCKET)})
    indexed = {item['new_path'] for item in entries if isinstance(item.get('new_path'), str)}
    indexed.update(row['old_path'] for row in rows if row['index_location'] == 'objects')
    old_pdfs = {path for path in derived if path.endswith('/document.pdf')}
    matched_targets = {row['new_path'] for row in rows}
    canonical_objects = {path for path in indexed if path.startswith('objects/')}
    unclassified_objects = sorted(path for path in objects if path.endswith('/document.pdf')
                                  and path not in matched_targets and path not in canonical_objects)
    return {'planned': rows, 'unresolved': unresolved,
            'unindexed_legacy_pdfs': sorted(old_pdfs - indexed),
            'unclassified_object_pdfs': unclassified_objects,
            'derived': summarize(derived), 'objects': summarize(objects),
            'deletion_authorized': False}


def candidate_manifest_paths(index, derived, objects, active_redirects=None):
    """Only inspect object manifests that could match an indexed legacy PDF."""
    if not isinstance(index, dict) or not isinstance(index.get('files'), list):
        raise ValueError('invalid canonical derived index')
    targets = set()
    paths = set()
    new_to_old = {new: old for old, new in (active_redirects or {}).items()}
    for entry in index['files']:
        if not isinstance(entry, dict) or not isinstance(entry.get('new_path'), str):
            raise ValueError('invalid canonical index entry')
        if entry['new_path'] in new_to_old:
            manifest = entry['new_path'].rsplit('/', 1)[0] + '/derived-manifest.json'
            if manifest in objects:
                paths.add(manifest)
            continue
        if not entry['new_path'].startswith('derived/'):
            continue
        validate_entry(entry)
        old = derived.get(entry['new_path'])
        if old:
            targets.add((entry['source_sha256'], old['xet_hash'], old['size']))
    for path, item in objects.items():
        match = NEW.fullmatch(path)
        if (match and match.group(1) == match.group(2)[:2]
                and (match.group(2), item['xet_hash'], item['size']) in targets):
            manifest = path.rsplit('/', 1)[0] + '/derived-manifest.json'
            if manifest in objects:
                paths.add(manifest)
    return sorted(paths)


def hub_inventory(api, prefix):
    result = {}
    for item in api.list_bucket_tree(BUCKET, prefix=prefix, recursive=True):
        if item.type != 'file':
            continue
        if (not isinstance(item.path, str) or not item.path.startswith(prefix)
                or type(item.size) is not int or item.size < 0
                or not isinstance(item.xet_hash, str) or not item.xet_hash):
            raise ValueError('invalid bucket inventory entry')
        result[item.path] = {'size': item.size, 'xet_hash': item.xet_hash}
    return result


def audit(store, api, *, workers=4, active_redirects=None):
    raw = store.read_bytes(BUCKET, INDEX)
    index = json.loads(raw)
    sidecar_raw = store.read_bytes(ASSETS, SIDECAR)
    sidecar = decode_sidecar(sidecar_raw)
    try:
        pointer_raw = store.read_bytes(ASSETS, POINTER)
        pointer = json.loads(pointer_raw)
    except FileNotFoundError:
        pointer_raw, pointer = None, None
    derived = hub_inventory(api, 'derived/')
    objects = hub_inventory(api, 'objects/')
    with ThreadPoolExecutor(max_workers=workers) as pool:
        manifest_paths = candidate_manifest_paths(index, derived, objects, active_redirects=active_redirects)
        def fetch(path):
            try:
                return path, store.read_bytes(BUCKET, path), None
            except Exception as error:
                return path, None, type(error).__name__
        manifest_results = dict((path, (raw, error)) for path, raw, error in pool.map(fetch, manifest_paths))
    def manifest(path):
        raw, error = manifest_results[path]
        if error:
            raise RuntimeError(error)
        return raw
    report = build_plan(index, derived, objects, manifest, sidecar=sidecar, active_redirects=active_redirects)
    # Re-read exact mutable roots; this does not make bucket inventories atomic.
    if store.read_bytes(BUCKET, INDEX) != raw or store.read_bytes(ASSETS, SIDECAR) != sidecar_raw:
        raise RuntimeError('canonical indexes changed while planning')
    if pointer_raw is None:
        try:
            store.read_bytes(ASSETS, POINTER)
        except FileNotFoundError:
            pass
        else:
            raise RuntimeError('v3 pointer appeared while planning')
    elif store.read_bytes(ASSETS, POINTER) != pointer_raw:
        raise RuntimeError('v3 pointer changed while planning')
    report.update(version=1, kind='pdf-page-bucket-audit', scanned_at=datetime.now(timezone.utc).isoformat(),
                  bucket=BUCKET, index_sha256=hashlib.sha256(raw).hexdigest(),
                  canonical_sidecar_sha256=hashlib.sha256(sidecar_raw).hexdigest(),
                  v3_generation=pointer.get('generation') if isinstance(pointer, dict) else None,
                  inventory_atomic=False, deletion_authorized=False)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    if not 1 <= args.workers <= 8:
        parser.error('--workers must be 1..8')
    try:
        result = audit(HubBucketStore(), HfApi(token=os.environ.get('HF_TOKEN')), workers=args.workers)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(encode(result) + b'\n')
        print(json.dumps({'planned': len(result['planned']), 'unresolved': len(result['unresolved']),
                          'unindexed_legacy_pdfs': len(result['unindexed_legacy_pdfs']),
                          'unclassified_object_pdfs': len(result['unclassified_object_pdfs']),
                          'derived': result['derived'], 'objects': result['objects'],
                          'deletion_authorized': False, 'output': str(args.output)}, ensure_ascii=False))
        return 0
    except Exception as error:
        print(f'{type(error).__name__}: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
