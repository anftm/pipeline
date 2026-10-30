#!/usr/bin/env python3
"""Read-only planning and reference simulation for a bounded OCR withdrawal.

This module cannot delete or mutate production. It explicitly distinguishes
external text copies from documents, and leaves current non-OCR Reader routes
and all historical CHM indexes untouched. A complete current graph and the
producer fence remain mandatory before any future mutation can be considered.
"""
from __future__ import annotations

import copy
import hashlib
import json
import posixpath
from pathlib import Path
import re
import time

try:
    from . import ccrd_ocr_exact_retirement as exact
    from . import reader_gc_graph as graph, reader_gc_stream as stream, reader_gc_collect as collector
    from . import ccrd_lifecycle as lifecycle
except ImportError:
    import ccrd_ocr_exact_retirement as exact
    import reader_gc_graph as graph
    import reader_gc_stream as stream
    import reader_gc_collect as collector
    import ccrd_lifecycle as lifecycle


ASSETS, PAGES, INPUT, CCRD = graph.BUCKETS
INVENTORY_SHA256 = '511e5aa26cd16ca4f190b6c86b7816ef3d186ed4ac99a85211d765f9ec8aec06'
INVENTORY_BYTES = 492873072
PRESERVED = f'control/ocr-preserved-reader-{exact.GENERATION}.json'
OBJECT_BASE = r'objects/[0-9a-f]{2}/[0-9a-f]{64}/[0-9a-f]{16}/'
PS_REBUILD = r'documents/pdf/ps-rebuild-20261025/[0-9a-f]{64}/(?:document\.pdf|document-manifest\.json)'
ALLOWED = {
    ASSETS: re.compile(PS_REBUILD),
    PAGES: re.compile(OBJECT_BASE + r'(?:ocr/page-[0-9]{6}\.json\.gz|'
        r'text/(?:page-[0-9]{6}\.json\.gz|book-text\.json\.gz|'
        r'search-[0-9]{6}-[0-9]{6}\.json\.gz|partition-[0-9]{6}-[0-9]{6}-manifest\.json)|'
        r'text-(?:layer|review)-manifest\.json)'),
    INPUT: re.compile(OBJECT_BASE + r'ocr-input/page-[0-9]{6}\.png'),
}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()


def inventory(path: Path):
    raw = path.read_bytes()
    if sha(raw) != INVENTORY_SHA256:
        raise ValueError('the reviewed OCR/PS inventory changed')
    value = json.loads(raw)
    rows = value.get('targets')
    if (value.get('kind') != 'expanded-external-text-ocr-ps-exact-inventory'
            or value.get('version') != 1 or not isinstance(rows, list)
            or len(rows) != 2710 or value.get('objects') != 2710
            or value.get('bytes') != INVENTORY_BYTES):
        raise ValueError('the reviewed OCR/PS inventory has unexpected metadata')
    expected_ccrd = {(CCRD, row['path'], row['bytes'], row['sha256']) for row in exact.load_targets()}
    seen, actual_ccrd = set(), set()
    for row in rows:
        bucket, path, size, digest = row.get('bucket'), row.get('path'), row.get('bytes'), row.get('sha256')
        if (bucket not in graph.BUCKETS or not isinstance(path, str) or (bucket, path) in seen
                or type(size) is not int or size < 1 or not isinstance(digest, str)
                or not re.fullmatch(r'[0-9a-f]{64}', digest)):
            raise ValueError('invalid or duplicate OCR/PS target')
        if bucket == CCRD:
            actual_ccrd.add((bucket, path, size, digest))
        elif not ALLOWED[bucket].fullmatch(path):
            raise ValueError('target is outside the approved OCR/PS namespaces')
        seen.add((bucket, path))
    if actual_ccrd != expected_ccrd or sum(row['bytes'] for row in rows) != INVENTORY_BYTES:
        raise ValueError('OCR/PS target identities or sizes changed')
    return value


def prepare(store, client, rows):
    """Rebuild the exact OCR withdrawal plan, preserving only non-target sources."""
    prior = exact.create_plan(store, client, exact.load_targets(), lambda _: None)
    targets = {(row['bucket'], row['path']) for row in rows}
    external = prior['updates']['external']['resources']
    kept = [ref for ref in external if (ref['bucket'], ref['path']) not in targets]
    if (len(external) != 2531 or len(kept) != 1 or kept[0]['bucket'] != PAGES
            or kept[0]['path'] != 'derived/migration-20261007/6f9f9f251a929f25e1b5be3beb32ec60/derived-manifest.json'):
        raise ValueError('outside-scope provenance differs from reviewed current-reader source')
    plan = copy.deepcopy(prior)
    plan['updates']['external']['resources'] = kept
    lifecycle.references(plan['updates']['external'], PRESERVED)
    plan['expanded_targets'] = copy.deepcopy(rows)
    plan['inventory_sha256'] = INVENTORY_SHA256
    plan['preserved_external_resources'] = len(kept)
    plan['retired_ocr_or_ps_external_resources'] = len(external) - len(kept)
    return plan


def simulate(store, plan, fence, *, workers=8, deadline_minutes=300, progress=None):
    """Find remaining target pins without changing a single production object."""
    overrides = {(CCRD, PRESERVED): encode(plan['updates']['external']),
                 (CCRD, exact.INTENT): encode(plan['updates']['intent']),
                 (CCRD, exact.IDS): encode(plan['updates']['ids']),
                 (ASSETS, exact.READER_CONSUMERS): encode(plan['updates']['reader_consumers'])}
    hidden = {(CCRD, exact.CURRENT)}

    class View:
        def __init__(self):
            self.object_sizes = store.object_sizes
            self.read_etags = {}
            self.root_etags = {}

        def list_files(self, bucket, prefixes):
            files = set(store.list_files(bucket, prefixes))
            files.difference_update(path for owner, path in hidden if owner == bucket)
            files.update(path for owner, path in overrides if owner == bucket and path.startswith(prefixes))
            return files

        def list_roots(self, bucket, parents):
            roots = {path: etag for path, etag in store.list_roots(bucket, parents).items()
                     if graph.is_root(path, bucket)}
            for owner, path in hidden:
                if owner == bucket:
                    roots.pop(path, None)
            for owner, path in overrides:
                if owner == bucket and graph.is_root(path, bucket):
                    roots[path] = None
            return roots

        def read_bytes(self, bucket, path):
            ref = (bucket, path)
            if ref in hidden:
                raise FileNotFoundError(path)
            return overrides[ref] if ref in overrides else store.read_bytes(bucket, path)

        def read_graph_json(self, bucket, path, parser=None):
            ref = (bucket, path)
            if ref in hidden:
                raise FileNotFoundError(path)
            if ref in overrides:
                return (parser or stream.graph_json)([overrides[ref]], path.endswith('.gz'))
            return store.read_graph_json(bucket, path, parser=parser)

        def digest(self, bucket, path):
            ref = (bucket, path)
            return sha(overrides[ref]) if ref in overrides else store.digest(bucket, path)

    fence.check()
    ref_graph = graph.ReferenceGraph(View(), workers=workers, max_rounds=40, progress=progress,
                                    deadline=time.monotonic() + deadline_minutes * 60)
    report = ref_graph.build()
    if not report['graph_complete'] or report['blockers'] or report.get('ambiguous_paths'):
        raise ValueError('current four-bucket graph incomplete or ambiguous: ' + repr(report['blockers'][:5]))
    targets = {(row['bucket'], row['path']) for row in plan['expanded_targets']}
    retained = targets & ref_graph.references
    preserved = {(r['bucket'], r['path']) for r in plan['updates']['external']['resources']}
    if not preserved <= ref_graph.references:
        raise ValueError('a current Reader source was not protected in simulation')
    origins = [{'bucket': origin[0], 'path': origin[1], 'targets': len(refs & targets)}
               for origin, refs in ref_graph.direct.items() if refs & targets]
    external_origins = external_target_origins(ref_graph, plan['expanded_targets'])
    fence.check()
    collector.verify_roots(ref_graph.store, ref_graph)
    controls, _ = exact.inspect_roots(store, None)
    if controls != plan['control_hashes_before']:
        raise ValueError('CCRD controls changed during reference simulation')
    if sha(store.read_bytes(ASSETS, exact.READER_CONSUMERS)) != plan['reader_consumer_sha256_before']:
        raise ValueError('Reader consumers changed during reference simulation')
    return {'kind': 'expanded-ocr-reference-simulation', 'graph_complete': True,
            'target_count': len(targets), 'retained_target_count': len(retained),
            'retained_targets': [f'{bucket}:{path}' for bucket, path in sorted(retained)],
            'remaining_origins': sorted(origins, key=lambda x: (x['bucket'], x['path'])),
            'non_target_origins': [{'bucket': b, 'path': p, 'targets': len(refs)}
                                   for (b, p), refs in sorted(external_origins.items())],
            'protected_non_ocr_refs': len(preserved), 'root_count': len(report['roots']),
            'deletion_authorized': False, 'remote_mutations': 0}


def external_target_origins(ref_graph, rows):
    """Conservatively find non-target origins that can protect exact targets.

    A target may be reached through an explicitly pinned path, or indirectly
    because the graph protects all siblings of an immutable bundle. A direct
    listing of target-to-target citations is therefore not enough to authorize
    retiring their SCC; expand every non-target document reference in the same
    way the graph's ``mark`` implementation does.
    """
    # Tree references are tracked separately from ``direct``; do not infer
    # external ownership solely from the direct-origin list.
    targets = {(row['bucket'], row['path']) for row in rows}
    directories = {}
    for bucket, path in targets:
        by_parent = directories.setdefault(bucket, {})
        parent = posixpath.dirname(path)
        while parent:
            by_parent.setdefault(parent, set()).add((bucket, path))
            parent = posixpath.dirname(parent)
    hits = {}
    for origin, refs in ref_graph.direct.items():
        if origin in targets:
            continue
        selected = set()
        for bucket, path in refs:
            exact_ref = (bucket, path)
            if exact_ref in targets:
                selected.add(exact_ref)
            if bucket == CCRD or graph.is_root(path, bucket):
                continue
            parent = posixpath.dirname(path)
            if not parent:
                continue
            selected.update(directories.get(bucket, {}).get(parent, set()))
        if selected:
            hits[origin] = selected
    # Tree roots are not recorded in ``direct`` by the scanner. Their source
    # therefore cannot be proven here; always treat their target closure as an
    # independent protection rather than guessing that it is target-internal.
    for bucket, root in ref_graph.trees:
        closure = directories.get(bucket, {}).get(root, set())
        if closure:
            hits[(bucket, root + '/<tree-protection>')] = set(closure)
    return hits
