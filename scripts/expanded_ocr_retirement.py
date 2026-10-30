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


def attest_root_fingerprints(report, ref_graph):
    """Return complete root membership and immutable read-time hashes."""
    expected_buckets = set(graph.BUCKETS)
    if (report.get('graph_complete') is not True or report.get('blockers')
            or report.get('ambiguous_paths') or set(ref_graph.files) != expected_buckets):
        raise ValueError('a complete four-bucket graph is required')
    root_members = {(bucket, path) for bucket, paths in ref_graph.files.items()
                    for path in paths if graph.is_root(path, bucket)}
    unrecorded = root_members - set(ref_graph.root_hashes) - ref_graph.retired_roots
    if unrecorded:
        raise ValueError('the complete graph did not read every active root')
    return {
        'root_members': [{'bucket': bucket, 'path': path} for bucket, path in sorted(root_members)],
        'root_sha256': [{'bucket': bucket, 'path': path, 'sha256': digest}
                        for (bucket, path), digest in sorted(ref_graph.root_hashes.items())],
        'retired_root_members': [{'bucket': bucket, 'path': path}
                                 for bucket, path in sorted(root_members & ref_graph.retired_roots)],
    }


def zero_root_target_proof(report, ref_graph, rows):
    """Prove reachability from every live root without trusting internal SCC pins.

    This is a read-only proof for the exact supplied set, not deletion authority.
    A target manifest reached from any non-target root is a blocker. Target
    manifests are not read merely because another target references them; their
    remaining internal edges disappear only if that whole target set is later
    retired as a single journalled operation.
    """
    attest = attest_root_fingerprints(report, ref_graph)
    if not isinstance(ref_graph, OriginReferenceGraph):
        raise ValueError('origin-aware tree tracing is required')
    untracked = ref_graph.trees - set(ref_graph.tree_origins)
    if untracked:
        raise ValueError('a protected tree has no recorded owner')
    targets = {(row['bucket'], row['path']) for row in rows}
    direct_targets = ref_graph.references & targets
    external = external_target_origins(ref_graph, rows)
    if direct_targets or external:
        return {'graph_complete': True, 'target_count': len(targets),
                'root_reachable_targets': len(direct_targets),
                'external_origins': len(external), 'all_active_roots_read': True,
                'untracked_trees': 0, 'target_exclusivity_proven': False,
                'deletion_authorized': False, **attest}
    return {'graph_complete': True, 'target_count': len(targets),
            'root_reachable_targets': 0, 'external_origins': 0,
            'all_active_roots_read': True, 'untracked_trees': 0,
            'target_exclusivity_proven': True, 'deletion_authorized': False, **attest}


def simulate(store, plan, fence, *, workers=8, deadline_minutes=300, progress=None,
             omit_internal_target_manifests=False):
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
    target_set = {(row['bucket'], row['path']) for row in plan['expanded_targets']}
    ref_graph = OriginReferenceGraph(View(), workers=workers, max_rounds=40, progress=progress,
                                     omit_internal=target_set if omit_internal_target_manifests else None,
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
    attestation = attest_root_fingerprints(report, ref_graph)
    zero_proof = (zero_root_target_proof(report, ref_graph, plan['expanded_targets'])
                  if omit_internal_target_manifests else None)
    return {'kind': 'expanded-ocr-reference-simulation', 'graph_complete': True,
            'target_count': len(targets), 'retained_target_count': len(retained),
            'retained_targets': [f'{bucket}:{path}' for bucket, path in sorted(retained)],
            'remaining_origins': sorted(origins, key=lambda x: (x['bucket'], x['path'])),
            'non_target_origins': [{'bucket': b, 'path': p, 'targets': len(refs)}
                                   for (b, p), refs in sorted(external_origins.items())],
            'tree_root_count': len(ref_graph.trees),
            'tree_roots_with_origin': len(ref_graph.tree_origins),
            'tree_roots_without_origin': len(ref_graph.trees - set(ref_graph.tree_origins)),
            'origin_tracking_complete': not bool(ref_graph.trees - set(ref_graph.tree_origins)),
            'internal_target_manifests_omitted': omit_internal_target_manifests,
            'target_exclusivity_proven': bool(zero_proof and zero_proof['target_exclusivity_proven']),
            'protected_non_ocr_refs': len(preserved), 'root_count': len(report['roots']),
            **attestation,
            'deletion_authorized': False, 'remote_mutations': 0}


class OriginReferenceGraph(graph.ReferenceGraph):
    """Record the source of both exact and automatically expanded tree pins.

    The base scanner's ``trees`` is a union: an identical directory may have
    several independent owners, and the first expansion wins. Record every
    request *before* the base implementation deduplicates that tree. Entries are
    retained for old root versions as well: stale pins are conservative.
    """
    def __init__(self, *args, omit_internal=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tree_origins = {}
        self.omit_internal = frozenset(omit_internal or ())
        self.omitted_manifest_reads = set()

    def load(self, ref):
        if ref in self.omit_internal and not graph.is_root(ref[1], ref[0]):
            # Target bytes were independently hashed in the inventory; a target
            # is treated as a terminal boundary in this hypothetical view.
            self.omitted_manifest_reads.add(ref)
            return None
        return super().load(ref)

    def mark(self, bucket, path, *, tree=False, external_pin=False):
        if (bucket in graph.BUCKETS and isinstance(path, str) and path
                and not path.startswith('/') and '\\' not in path and '\0' not in path
                and not any(part in {'', '.', '..'} for part in path.split('/'))
                and bucket != CCRD and bucket in self.files
                and (tree or path in self.files[bucket])):
            root = path if tree else posixpath.dirname(path)
            if root and not graph.is_root(path):
                self.tree_origins.setdefault((bucket, root), set()).add(self.origin)
        return super().mark(bucket, path, tree=tree, external_pin=external_pin)


def external_target_origins(ref_graph, rows):
    """Conservatively find non-target origins that can protect exact targets.

    A target may be reached through an explicitly pinned path, or indirectly
    because the graph protects all siblings of an immutable bundle. A direct
    listing of target-to-target citations is therefore not enough to authorize
    retiring their SCC; expand every non-target document reference in the same
    way the graph's ``mark`` implementation does.
    """
    if not hasattr(ref_graph, 'tree_origins'):
        raise ValueError('tree protection origins were not recorded')
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
    # An unobserved or unowned tree remains a blocker; only a tree exclusively
    # initiated by target documents is part of the proposed retirement SCC.
    for bucket, root in ref_graph.trees:
        closure = directories.get(bucket, {}).get(root, set())
        if not closure:
            continue
        owners = ref_graph.tree_origins.get((bucket, root))
        if not owners:
            hits[(bucket, root + '/<unknown-tree-owner>')] = set(closure)
            continue
        for owner in owners:
            if owner is None:
                hits[(bucket, root + '/<unknown-tree-owner>')] = set(closure)
            elif owner not in targets:
                hits.setdefault(owner, set()).update(closure)
    return hits
