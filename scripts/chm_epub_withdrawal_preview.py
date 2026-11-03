#!/usr/bin/env python3
"""Read-only preview of a narrowly scoped CHM legacy EPUB withdrawal.

This module never writes a bucket, publishes a Reader generation, or deletes
data. Its planned changes must not be applied until both consumers, old
rollback roots, all four storage domains, and producer isolation are verified.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import time

try:
    from . import reader_lifecycle as lifecycle
    from . import reader_gc_collect as collector, reader_gc_graph as graph
    from . import reader_gc_stream as graph_stream
except ImportError:
    import reader_lifecycle as lifecycle
    import reader_gc_collect as collector
    import reader_gc_graph as graph
    import reader_gc_stream as graph_stream


ASSETS = 'vomebook/reader-assets-v2'
PREFIX = 'native/ebook/chm/'
FALLBACK = re.compile(r'native/ebook/chm/[0-9a-f]{64}/document\.epub')
MANIFEST = re.compile(r'chapters/ebook/chm/([0-9a-f]{64})/[0-9a-f]{16}/chapter-manifest\.json')
INVENTORY_SHA256 = 'dec3b852cfe46c4c534c105d80b8eba8a57468b2e4550925a714d53d980a6e69'
DATA_BYTES = 981448500


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def load_inventory(path: Path):
    raw = path.read_bytes()
    if sha(raw) != INVENTORY_SHA256:
        raise ValueError('reviewed CHM inventory changed')
    data = json.loads(raw)
    rows = data.get('targets')
    if (data.get('kind') != 'chm-historical-epub-inventory' or not isinstance(rows, list)
            or len(rows) != 972 or data.get('object_count') != 972):
        raise ValueError('invalid reviewed CHM inventory')
    keys = set()
    for row in rows:
        if (not isinstance(row, dict) or row.get('bucket') != ASSETS
                or not isinstance(row.get('path'), str) or not FALLBACK.fullmatch(row['path'])
                or type(row.get('bytes')) is not int or row['bytes'] < 1
                or not isinstance(row.get('sha256'), str)
                or not re.fullmatch(r'[0-9a-f]{64}', row['sha256'])
                or row['path'] in keys):
            raise ValueError('invalid or duplicate historical EPUB target')
        keys.add(row['path'])
    if sum(r['bytes'] for r in rows) != DATA_BYTES or data.get('logical_bytes') != DATA_BYTES:
        raise ValueError('reviewed historical EPUB byte total changed')
    return data


def preview_shards(indexes, canonical, rows):
    """Retire only reviewed EPUB fields in old shards, never their chapter data."""
    targets = {row['path'] for row in rows}
    if len(targets) != len(rows) or any(not FALLBACK.fullmatch(path) for path in targets):
        raise ValueError('invalid exact historical EPUB target set')
    canonical_keys = {entry.get('key') for entry in canonical['files']}
    output, removed, changed_entries = {}, set(), 0
    for path, source in indexes.items():
        if not re.fullmatch(r'chapters/ebook/chm/index-0[0-7]\.json', path):
            raise ValueError('unexpected CHM shard index path')
        if (source.get('version') != 1 or source.get('kind') != 'ebook-chapter-stream-index'
                or not isinstance(source.get('files'), list)):
            raise ValueError('invalid CHM shard index')
        value, keys = copy.deepcopy(source), set()
        for entry in value['files']:
            if not isinstance(entry, dict) or not isinstance(entry.get('key'), str):
                raise ValueError('invalid shard entry')
            key = entry['key']
            if key in keys or key not in canonical_keys or not key.lower().endswith('.chm'):
                raise ValueError('unexpected or duplicate CHM shard source')
            keys.add(key)
            manifest = entry.get('manifest')
            match = MANIFEST.fullmatch(manifest) if isinstance(manifest, str) else None
            if (not match or entry.get('source_sha256') != match[1]
                    or entry.get('bucket') != ASSETS or entry.get('mode') != 'chapter-stream'
                    or entry.get('root') != manifest.rsplit('/', 1)[0]
                    or entry.get('search_index') != entry['root'] + '/epub-search-index.json.gz'):
                raise ValueError('invalid old shard chapter mapping')
            fallback = entry.get('fallback')
            if fallback is not None:
                if fallback not in targets or entry.get('object') not in (None, fallback):
                    raise ValueError('shard fallback outside the reviewed inventory')
                removed.add(fallback)
                changed_entries += 1
                for field in ('fallback', 'object', 'fallback_sha256', 'fallback_bytes', 'fallback_source_sha256'):
                    entry.pop(field, None)
            elif entry.get('object') is not None:
                raise ValueError('shard object has no reviewed fallback')
        output[path] = value
    return {'indexes': output, 'entries_changed': changed_entries,
            'unique_fallbacks_removed': len(removed), 'remote_mutations': 0}


def preview(index, sidecar, catalog, rows):
    targets = {r['path'] for r in rows}
    if len(targets) != len(rows) or any(not FALLBACK.fullmatch(p) for p in targets):
        raise ValueError('invalid exact historical EPUB target set')
    if (index.get('version') != 1 or index.get('kind') != 'ebook-chapter-stream-index'
            or not isinstance(index.get('files'), list)
            or sidecar.get('v') != 1 or not isinstance(sidecar.get('f'), dict)):
        raise ValueError('unexpected CHM index or Reader sidecar schema')
    updated_index, updated_sidecar = copy.deepcopy(index), copy.deepcopy(sidecar)
    selected = {}
    for entry in updated_index['files']:
        if not isinstance(entry, dict) or not isinstance(entry.get('key'), str):
            raise ValueError('invalid CHM index entry')
        key = entry['key']
        if key in selected or not key.lower().endswith('.chm'):
            raise ValueError('duplicated or non-CHM source')
        manifest = entry.get('manifest')
        match = MANIFEST.fullmatch(manifest) if isinstance(manifest, str) else None
        if (not match or entry.get('source_sha256') != match[1]
                or entry.get('bucket') != ASSETS or entry.get('mode') != 'chapter-stream'
                or entry.get('root') != manifest.rsplit('/', 1)[0]
                or entry.get('search_index') != entry['root'] + '/epub-search-index.json.gz'
                or type(entry.get('chapter_count')) is not int or entry['chapter_count'] < 1):
            raise ValueError('CHM chapter mapping is incomplete or inconsistent')
        fallback = entry.get('fallback')
        if fallback is not None:
            if fallback not in targets or entry.get('object') not in (None, fallback):
                raise ValueError('CHM fallback is outside the exact reviewed inventory')
            for field in ('fallback', 'object', 'fallback_sha256', 'fallback_bytes', 'fallback_source_sha256'):
                entry.pop(field, None)
        elif entry.get('object') is not None:
            raise ValueError('CHM object exists without a reviewed fallback')
        selected[key] = (entry, fallback)
    for key, (entry, fallback) in selected.items():
        mapping = updated_sidecar['f'].get(key)
        if (not isinstance(mapping, dict) or mapping.get('m') != 'e' or mapping.get('s') != 2
                or mapping.get('p') != entry['manifest'] or mapping.get('c') != entry['manifest']
                or mapping.get('b') != ASSETS or mapping.get('cb') != ASSETS
                or mapping.get('f') != fallback):
            raise ValueError('CHM sidecar differs from its indexed chapter mapping')
        if fallback is not None:
            mapping.pop('f')
            if mapping.get('fb') is not None:
                raise ValueError('unexpected explicit fallback bucket')
    for key, mapping in updated_sidecar['f'].items():
        if not isinstance(mapping, dict):
            raise ValueError('invalid Reader sidecar mapping')
        if any(isinstance(value, str) and value.startswith(PREFIX) for value in mapping.values()):
            raise ValueError('another sidecar mapping still names a historical CHM EPUB')
    before = {(r['bucket'], r['path']) for r in lifecycle.sidecar_references(sidecar)}
    after = {(r['bucket'], r['path']) for r in lifecycle.sidecar_references(updated_sidecar)}
    removed = {(ASSETS, fallback) for _, fallback in selected.values() if fallback is not None}
    if before - after != removed or after - before:
        raise ValueError('proposed sidecar changes references beyond CHM fallbacks')
    new_catalog, generation = lifecycle.catalog_generation(updated_sidecar, catalog)
    old_generation = (catalog.get('current') or {}).get('generation')
    if not old_generation or old_generation not in new_catalog['generations']:
        raise ValueError('old Reader generation must stay protected until both deployments acknowledge')
    return {
        'index': updated_index, 'sidecar': updated_sidecar, 'catalog': new_catalog,
        'generation': generation, 'previous_generation': old_generation,
        'entries_changed': sum(fallback is not None for _, fallback in selected.values()),
        'unique_fallbacks_removed': len(removed),
        'historical_objects_not_in_current_index': len(targets - {p for _, p in removed}),
        'remaining_targets': len(targets), 'remote_mutations': 0,
    }


def simulate_retired_indexes(store, result, shards, rows, fence, *, workers=8, deadline_minutes=300):
    """Report all four-domain pins under a hypothetical CHM-only metadata update.

    V3 history and consumer receipts are left intact intentionally; any pins
    they own are reported, never silently removed or treated as deletion
    authority. This view cannot mutate its backing store.
    """
    def json_bytes(value):
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()

    overrides = {(ASSETS, 'chapters/ebook/chm/index.json'): json_bytes(result['index']),
                 (ASSETS, 'reader-index/reader_assets.json.gz'): json_bytes(result['sidecar']),
                 (ASSETS, 'reader-index/reader_catalog.json'): json_bytes(result['catalog'])}
    overrides.update({(ASSETS, path): json_bytes(value) for path, value in shards['indexes'].items()})
    # The sidecar override is encoded in the same wire format its path requires.
    import gzip
    ref = (ASSETS, 'reader-index/reader_assets.json.gz')
    overrides[ref] = gzip.compress(overrides[ref], mtime=0)

    class View:
        def __init__(self):
            self.object_sizes = store.object_sizes
            self.read_etags = {}
            self.root_etags = {}

        def list_files(self, bucket, prefixes):
            files = set(store.list_files(bucket, prefixes))
            files.update(path for owner, path in overrides if owner == bucket and path.startswith(prefixes))
            return files

        def list_roots(self, bucket, parents):
            roots = {path: etag for path, etag in store.list_roots(bucket, parents).items()
                     if graph.is_root(path, bucket)}
            for owner, path in overrides:
                if owner == bucket and graph.is_root(path, bucket):
                    roots[path] = None
            return roots

        def read_bytes(self, bucket, path):
            ref = (bucket, path)
            return overrides[ref] if ref in overrides else store.read_bytes(bucket, path)

        def read_graph_json(self, bucket, path, parser=None):
            ref = (bucket, path)
            if ref in overrides:
                return (parser or graph_stream.graph_json)([overrides[ref]], path.endswith('.gz'))
            return store.read_graph_json(bucket, path, parser=parser)

        def digest(self, bucket, path):
            ref = (bucket, path)
            return sha(overrides[ref]) if ref in overrides else store.digest(bucket, path)

    before = {ref: store.digest(*ref) for ref in overrides}
    fence.check()
    reference_graph = graph.ReferenceGraph(View(), workers=workers, max_rounds=40,
                                           deadline=time.monotonic() + deadline_minutes * 60)
    report = reference_graph.build()
    if not report['graph_complete'] or report['blockers'] or report.get('ambiguous_paths'):
        raise ValueError('full four-bucket graph incomplete or ambiguous: ' + repr(report['blockers'][:5]))
    targets = {(ASSETS, row['path']) for row in rows}
    retained = targets & reference_graph.references
    origins = []
    for origin, refs in reference_graph.direct.items():
        if refs & targets:
            origins.append({'bucket': origin[0], 'path': origin[1], 'targets': len(refs & targets)})
    fence.check()
    collector.verify_roots(reference_graph.store, reference_graph)
    if any(store.digest(*ref) != digest for ref, digest in before.items()):
        raise ValueError('underlying CHM controls changed during simulated scan')
    return {'graph_complete': True, 'mode': 'read-only-simulated-index-retirement',
            'target_count': len(targets), 'targets_still_referenced': len(retained),
            'remaining_origin_count': len(origins), 'remaining_origins': origins,
            'root_count': len(report['roots']), 'scanned_at': report['scanned_at'],
            'deletion_authorized': False, 'remote_mutations': 0}
