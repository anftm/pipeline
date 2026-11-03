#!/usr/bin/env python3
"""Pure validation for a possible CHM subset retirement.

This module is intentionally non-executable: it has no network client, writes,
delete calls, workflow entrypoint, or ability to authorize production. It
validates exact associations so a later separately reviewed transaction can
fail closed before considering the existing four-bucket/consumer barriers.
"""
from __future__ import annotations

import hashlib
import json
import re

try:
    from . import chm_epub_publication_preflight as chm
except ImportError:
    import chm_epub_publication_preflight as chm

ASSETS = chm.ASSETS
SOURCE = re.compile(r'chapters/ebook/chm/[0-9a-f]{64}/[0-9a-f]{16}/chapter-manifest\.json')
TARGET = re.compile(r'native/ebook/chm/[0-9a-f]{64}/document\.epub')


def verify_exact_partition(rows, batches, selected, withheld):
    """The two new lists partition every unsubmitted target, never old receipts."""
    if not isinstance(rows, list):
        raise ValueError('invalid exact CHM inventory')
    approved = {}
    for row in rows:
        path = row.get('path') if isinstance(row, dict) else None
        if (not isinstance(path, str) or not TARGET.fullmatch(path) or row.get('bucket') != ASSETS
                or path in approved or type(row.get('bytes')) is not int or row['bytes'] < 1
                or not re.fullmatch(r'[0-9a-f]{64}', str(row.get('sha256', '')))):
            raise ValueError('invalid or duplicate exact CHM inventory')
        approved[path] = row
    if not approved or not isinstance(batches, list):
        raise ValueError('missing exact CHM inventory or prior receipts')
    submitted = set()
    for batch in batches:
        if not isinstance(batch, dict):
            raise ValueError('invalid old CHM receipt')
        paths = batch.get('paths')
        if (batch.get('bucket') != ASSETS or batch.get('status') != 'verified-absent'
                or not isinstance(paths, list) or not paths or len(paths) > 100
                or any(not isinstance(path, str) for path in paths)
                or len(paths) != len(set(paths)) or any(p not in approved or p in submitted for p in paths)):
            raise ValueError('invalid or duplicate old CHM receipt')
        submitted.update(paths)
    groups = []
    for group in (selected, withheld):
        if not isinstance(group, list):
            raise ValueError('subset is not a list')
        values = {}
        for row in group:
            if not isinstance(row, dict):
                raise ValueError('invalid subset row')
            path = row.get('path')
            if not isinstance(path, str) or path in values or path not in approved or row != approved[path] or path in submitted:
                raise ValueError('subset row differs from the reviewed exact inventory')
            values[path] = row
        groups.append(values)
    chosen, retained = groups
    if not chosen or not retained or set(chosen) & set(retained) or set(chosen) | set(retained) != set(approved) - submitted:
        raise ValueError('subset does not partition every unsubmitted target')
    return {'old_receipts': len(submitted), 'old_receipt_bytes': sum(approved[path]['bytes'] for path in submitted),
            'selected': len(chosen), 'withheld': len(retained),
            'selected_bytes': sum(row['bytes'] for row in chosen.values()),
            'withheld_bytes': sum(row['bytes'] for row in retained.values()),
            'remote_mutations': 0, 'deletion_authorized': False}


def verify_subset_graph_evidence(proof, selected, withheld):
    """Check a supplied four-bucket proof; does not create or authenticate it.

    Unlike the original all-972 barrier, this requires the exact withheld
    objects to be protected while every selected object has zero references.
    A caller must independently establish the proof is fresh, constructed under
    the right locks, and bound to exact remote control bytes.
    """
    try:
        from . import reader_gc_graph as graph
    except ImportError:
        import reader_gc_graph as graph
    groups = []
    for rows in (selected, withheld):
        if not isinstance(rows, list) or not rows:
            raise ValueError('empty selected or withheld graph scope')
        refs = set()
        for row in rows:
            path = row.get('path') if isinstance(row, dict) else None
            if (not isinstance(path, str) or not TARGET.fullmatch(path) or row.get('bucket') != ASSETS
                    or (ASSETS, path) in refs):
                raise ValueError('invalid or duplicate graph target')
            refs.add((ASSETS, path))
        groups.append(refs)
    chosen, retained = groups
    if chosen & retained:
        raise ValueError('graph target scopes overlap')
    if (not isinstance(proof, dict) or proof.get('mode') != 'actual'
            or proof.get('graph_complete') is not True or proof.get('buckets') != list(graph.BUCKETS)
            or proof.get('root_recheck_complete') is not True or proof.get('reader_acceptance_complete') is not True
            or proof.get('consumer_receipts') != {'hf': True, 'pages': True}
            or proof.get('rollback_alias_count') != 5 or proof.get('blockers') != []
            or proof.get('ambiguous_paths') != [] or proof.get('remote_mutations') != 0):
        raise ValueError('actual four-bucket graph or consumer proof is incomplete')
    protected = proof.get('protected_targets')
    unreferenced = proof.get('unreferenced_targets')
    def exact_refs(values):
        if not isinstance(values, list) or any(not isinstance(item, dict) or set(item) != {'bucket', 'path'}
                                               or item['bucket'] != ASSETS or not isinstance(item['path'], str)
                                               or not TARGET.fullmatch(item['path']) for item in values):
            raise ValueError('invalid graph target references')
        refs = {(row['bucket'], row['path']) for row in values}
        if len(refs) != len(values):
            raise ValueError('duplicate graph target references')
        return refs
    if exact_refs(protected) != retained or exact_refs(unreferenced) != chosen:
        raise ValueError('actual graph does not match selected and withheld sets')
    for field in ('root_sha256', 'closure_sha256'):
        values = proof.get(field)
        if (not isinstance(values, list) or not values or any(
                not isinstance(value, dict) or value.get('bucket') not in graph.BUCKETS
                or not isinstance(value.get('path'), str) or not value['path']
                or not isinstance(value.get('sha256'), str)
                or not re.fullmatch(r'[0-9a-f]{64}', value['sha256']) for value in values)):
            raise ValueError('actual graph SHA-256 evidence is incomplete')
        if len({(value['bucket'], value['path']) for value in values}) != len(values):
            raise ValueError('duplicate graph SHA-256 evidence')
    roots = {(row['bucket'], row['path']): row['sha256'] for row in proof['root_sha256']}
    closure = {(row['bucket'], row['path']): row['sha256'] for row in proof['closure_sha256']}
    if any(closure.get(ref) != sha for ref, sha in roots.items()):
        raise ValueError('graph root hashes are not bound to the closure')
    return {'selected': len(chosen), 'withheld': len(retained), 'roots': len(roots), 'closure': len(closure),
            'remote_mutations': 0, 'deletion_authorized': False}


def verify_subset_cursor(selected, withheld, state):
    """Check a supplied cursor's exact order and claimed subset completion.

    This is not durable storage, CAS, a receipt, or a remote absence check. A
    caller must bind the cursor to an immutable plan and reread every target.
    """
    if not isinstance(selected, list) or not selected or not isinstance(withheld, list) or not withheld:
        raise ValueError('invalid selected or withheld cursor scope')
    groups = []
    for rows in (selected, withheld):
        values = []
        for row in rows:
            path = row.get('path') if isinstance(row, dict) else None
            if not isinstance(path, str) or not TARGET.fullmatch(path) or row.get('bucket') != ASSETS:
                raise ValueError('cursor scope contains an invalid target')
            values.append(path)
        if len(values) != len(set(values)):
            raise ValueError('cursor scope contains duplicate targets')
        groups.append(values)
    chosen, kept = groups
    if set(chosen) & set(kept) or chosen != sorted(chosen):
        raise ValueError('cursor scopes overlap or are not in deterministic order')
    if (not isinstance(state, dict) or not isinstance(state.get('receipts'), list)
            or type(state.get('subset_complete')) is not bool or state.get('original_complete') is not False
            or state.get('producer_restore_authorized') is not False):
        raise ValueError('invalid subset cursor or original transaction claim')
    verified = []
    for batch in state['receipts']:
        paths = batch.get('paths') if isinstance(batch, dict) else None
        if (not isinstance(batch, dict) or batch.get('bucket') != ASSETS or batch.get('status') != 'verified-absent'
                or not isinstance(paths, list) or not paths or len(paths) > 100
                or any(not isinstance(path, str) for path in paths)):
            raise ValueError('subset receipt is malformed')
        verified.extend(paths)
    if len(verified) != len(set(verified)) or verified != chosen[:len(verified)]:
        raise ValueError('subset receipts are duplicated or out of exact order')
    if len(verified) > len(chosen):
        raise ValueError('subset receipts exceed exact target inventory')
    pending = state.get('pending')
    if pending is not None:
        if (not isinstance(pending, dict) or pending.get('bucket') != ASSETS
                or pending.get('status') != 'submitted' or not isinstance(pending.get('paths'), list)
                or not pending['paths'] or len(pending['paths']) > 100
                or pending['paths'] != chosen[len(verified):len(verified) + len(pending['paths'])]):
            raise ValueError('subset pending intent is outside exact order')
    if state['subset_complete'] and (len(verified) != len(chosen) or pending is not None):
        raise ValueError('subset cannot be complete with missing or pending targets')
    return {'verified': len(verified), 'pending': len(pending['paths']) if pending else 0,
            'subset_complete': state['subset_complete'], 'original_complete': False,
            'producer_restore_authorized': False, 'remote_mutations': 0, 'deletion_authorized': False}


def compare_subset_closure(previous, actual, *, old_retention_ref, planned_retention_sha256):
    """Permit only one explicitly supplied root-byte change in supplied proofs.

    This is a data comparator, not a proof producer. Both inputs must come from
    real, independently authenticated graph traversals under isolation. The
    retention object also needs its own exact semantic diff and CAS audit; a
    matching SHA alone does not prove authorship or exclude ETag ABA.
    """
    try:
        from . import reader_gc_graph as graph
    except ImportError:
        import reader_gc_graph as graph
    if (not isinstance(old_retention_ref, tuple) or len(old_retention_ref) != 2
            or old_retention_ref[0] != ASSETS
            or old_retention_ref[1] != 'reader-index/chm-epub-withdrawal-retention.json'
            or not isinstance(planned_retention_sha256, str)
            or not re.fullmatch(r'[0-9a-f]{64}', planned_retention_sha256)):
        raise ValueError('invalid exact retention transition identity')

    def pairs(values, field, *, digest=False):
        if not isinstance(values, list):
            raise ValueError('invalid ' + field)
        result = {}
        for value in values:
            if (not isinstance(value, dict) or value.get('bucket') not in graph.BUCKETS
                    or not isinstance(value.get('path'), str) or not value['path']
                    or digest and (not isinstance(value.get('sha256'), str)
                                   or not re.fullmatch(r'[0-9a-f]{64}', value['sha256']))):
                raise ValueError('invalid ' + field + ' entry')
            ref = (value['bucket'], value['path'])
            if ref in result:
                raise ValueError('duplicate ' + field + ' entry')
            result[ref] = value['sha256'] if digest else True
        return result

    for proof in (previous, actual):
        if (not isinstance(proof, dict) or proof.get('mode') != 'actual' or proof.get('graph_complete') is not True
                or proof.get('blockers') != [] or proof.get('ambiguous_paths') != []
                or proof.get('root_recheck_complete') is not True or proof.get('reader_acceptance_complete') is not True
                or proof.get('remote_mutations') != 0):
            raise ValueError('actual graph proof incomplete')
    if (previous.get('target_count') != 972 or previous.get('retained_targets') != 0
            or previous.get('origins') != [] or not isinstance(previous.get('root_sha256'), list)
            or previous.get('root_count') != len(previous['root_sha256'])):
        raise ValueError('original all-target zero-reference graph evidence is incomplete')
    old_members = pairs(previous.get('root_members'), 'old root members')
    new_members = pairs(actual.get('root_members'), 'new root members')
    old_roots = pairs(previous.get('root_sha256'), 'old root SHA', digest=True)
    new_roots = pairs(actual.get('root_sha256'), 'new root SHA', digest=True)
    old_closure = pairs(previous.get('closure_sha256'), 'old closure SHA', digest=True)
    new_closure = pairs(actual.get('closure_sha256'), 'new closure SHA', digest=True)
    old_retired = pairs(previous.get('retired_roots'), 'old retired roots')
    new_retired = pairs(actual.get('retired_roots'), 'new retired roots')
    if (not old_members or set(old_members) != set(new_members)
            or not set(old_retired) <= set(new_retired) or not set(new_retired) <= set(new_members)
            or not (set(old_members) - set(old_retired)) <= set(old_roots)
            or not (set(new_members) - set(new_retired)) <= set(new_roots)
            or not set(old_roots) <= set(old_members) or not set(new_roots) <= set(new_members)
            or old_retention_ref not in old_members or old_retention_ref in old_retired
            or old_retention_ref in new_retired or old_retention_ref not in old_roots
            or old_retention_ref not in old_closure or old_retention_ref not in new_roots
            or old_retention_ref not in new_closure):
        raise ValueError('graph roots or retention identity changed')
    if (old_roots[old_retention_ref] != old_closure[old_retention_ref]
            or new_roots[old_retention_ref] != planned_retention_sha256
            or new_closure[old_retention_ref] != planned_retention_sha256):
        raise ValueError('retention root SHA does not bind both closures')
    old_root_other = {ref: sha for ref, sha in old_roots.items() if ref != old_retention_ref}
    new_root_other = {ref: sha for ref, sha in new_roots.items() if ref != old_retention_ref}
    old_closure_other = {ref: sha for ref, sha in old_closure.items() if ref != old_retention_ref}
    new_closure_other = {ref: sha for ref, sha in new_closure.items() if ref != old_retention_ref}
    if (new_root_other != old_root_other or not set(new_closure_other) <= set(old_closure_other)
            or any(old_closure_other[ref] != sha for ref, sha in new_closure_other.items())
            or any(old_closure.get(ref) != sha for ref, sha in old_roots.items())
            or any(new_closure.get(ref) != sha for ref, sha in new_roots.items())):
        raise ValueError('non-retention graph closure changed or grew')
    return {'root_members': len(old_members), 'old_closure': len(old_closure), 'new_closure': len(new_closure),
            'retention_sha256': planned_retention_sha256, 'remote_mutations': 0, 'deletion_authorized': False}


def compare_subset_overlay_graph(previous, simulated, withheld, *, before_raw):
    """Compare old actual closure with a real-store graph's in-memory overlay.

    This cannot turn a simulation into actual evidence or deletion authority.
    The caller must independently authenticate the traversal, original bytes,
    controls and raw producer isolation; no remote object is written here.
    """
    try:
        from . import chm_subset_protection as protection
    except ImportError:
        import chm_subset_protection as protection
    if (not isinstance(simulated, dict) or simulated.get('mode') != 'hypothetical-overlay'
            or simulated.get('graph_complete') is not True or simulated.get('blockers') != []
            or simulated.get('ambiguous_paths') != [] or simulated.get('root_recheck_complete') is not True
            or simulated.get('reader_acceptance_complete') is not True
            or simulated.get('remote_mutations') != 0 or simulated.get('deletion_authorized') is not False):
        raise ValueError('hypothetical graph is incomplete or claims authorization')
    planned = protection.prepare(before_raw, withheld)
    retention = (ASSETS, 'reader-index/chm-epub-withdrawal-retention.json')
    roots = previous.get('root_sha256') if isinstance(previous, dict) else None
    previous_retention = [entry for entry in roots if isinstance(entry, dict)
                          and (entry.get('bucket'), entry.get('path')) == retention] if isinstance(roots, list) else []
    if len(previous_retention) != 1 or previous_retention[0].get('sha256') != planned['before_sha256']:
        raise ValueError('overlay before bytes do not match the original graph retention SHA')
    comparable = {**simulated, 'mode': 'actual'}
    compared = compare_subset_closure(previous, comparable, old_retention_ref=retention,
                                       planned_retention_sha256=planned['after_sha256'])
    return {**compared, 'mode': 'hypothetical-overlay-only', 'old_retention_sha256': planned['before_sha256'],
            'new_retention_sha256': planned['after_sha256'], 'remote_mutations': 0, 'deletion_authorized': False}


def verify_route_limit_inputs(rows, selected, history, current, index, evidence):
    """Return exact manifest identities for all selected old/current routes.

    This only constructs a checklist. Callers must freshly GET and hash every
    manifest and require the deployed Reader limit validator to pass; saved
    evidence or a source hash is not evidence of current bytes/readability.
    """
    if not isinstance(rows, list) or not isinstance(selected, list):
        raise ValueError('invalid route target inventory')
    approved = {}
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get('path'), str)
                or not TARGET.fullmatch(row['path']) or row.get('bucket') != ASSETS or row['path'] in approved):
            raise ValueError('invalid or duplicate route target')
        approved[row['path']] = row
    chosen = {}
    for row in selected:
        path = row.get('path') if isinstance(row, dict) else None
        if not isinstance(path, str) or path in chosen or path not in approved or row != approved[path]:
            raise ValueError('selected target differs from exact inventory')
        chosen[path] = row
    if not chosen or not isinstance(history, dict) or not history:
        raise ValueError('selected targets or historical routes missing')
    if not isinstance(current, dict) or current.get('v') != 1 or not isinstance(current.get('f'), dict):
        raise ValueError('invalid current sidecar')
    if not isinstance(index, dict) or not isinstance(index.get('files'), list):
        raise ValueError('invalid chapter index')
    entries = {}
    for item in index['files']:
        if not isinstance(item, dict) or not isinstance(item.get('key'), str) or item['key'] in entries:
            raise ValueError('invalid or duplicate chapter index key')
        entries[item['key']] = item
    if not isinstance(evidence, dict) or not evidence or any(
            not isinstance(path, str) or not SOURCE.fullmatch(path) or not isinstance(sha, str)
            or not re.fullmatch(r'[0-9a-f]{64}', sha) for path, sha in evidence.items()):
        raise ValueError('invalid chapter manifest evidence')
    references = {path: set() for path in chosen}
    keys = {path: set() for path in chosen}
    for generation, sidecar in history.items():
        if (not isinstance(generation, str) or not isinstance(sidecar, dict) or sidecar.get('v') != 1
                or not isinstance(sidecar.get('f'), dict)):
            raise ValueError('invalid historical sidecar')
        for key, route in sidecar['f'].items():
            if not isinstance(route, dict):
                raise ValueError('invalid historical route')
            target = route.get('f')
            if target is not None and not isinstance(target, str):
                raise ValueError('invalid historical fallback')
            if target not in chosen:
                continue
            old = route.get('c')
            if (not isinstance(key, str) or not key.lower().endswith('.chm')
                    or not isinstance(old, str) or not SOURCE.fullmatch(old)
                    or route.get('p') != old or route.get('b') != ASSETS or route.get('cb') != ASSETS
                    or route.get('m') != 'e' or route.get('s') != 2 or old not in evidence):
                raise ValueError('historical CHM route is not evidenced')
            now, indexed = current['f'].get(key), entries.get(key)
            if not isinstance(now, dict) or not isinstance(indexed, dict):
                raise ValueError('current CHM route or index is missing')
            new = now.get('c')
            if (not isinstance(new, str) or not SOURCE.fullmatch(new) or now.get('p') != new
                    or now.get('b') != ASSETS or now.get('cb') != ASSETS or now.get('m') != 'e'
                    or now.get('s') != 2 or new not in evidence or indexed.get('manifest') != new
                    or indexed.get('bucket') != ASSETS or indexed.get('mode') != 'chapter-stream'
                    or indexed.get('source_sha256') != new.split('/')[3]
                    or indexed.get('root') != new.rsplit('/', 1)[0]
                    or indexed.get('search_index') != new.rsplit('/', 1)[0] + '/epub-search-index.json.gz'
                    or type(indexed.get('chapter_count')) is not int or indexed['chapter_count'] < 1):
                raise ValueError('current CHM route or index is inconsistent')
            references[target].update((old, new))
            keys[target].add(key)
    if any(not links for links in references.values()):
        raise ValueError('selected target lacks a verified historical fallback route')
    manifests = {path: evidence[path] for paths in references.values() for path in paths}
    return {'manifests': dict(sorted(manifests.items())),
            'routes': {path: {'keys': sorted(keys[path]), 'manifests': sorted(references[path])}
                       for path in sorted(chosen)},
            'remote_mutations': 0, 'deletion_authorized': False}


def verify_manifest_bytes(manifest_checklist, raw_by_path):
    """Validate *provided* bytes and deployed size/schema; never fetch or mutate.

    An exact byte mapping is mandatory: omitted, extra, malformed, or larger
    manifests cannot be counted as a pass. This cannot authenticate where the
    caller obtained the bytes or prove chapter bodies/resources are present.
    """
    if (not isinstance(manifest_checklist, dict) or not manifest_checklist
            or not isinstance(raw_by_path, dict) or set(raw_by_path) != set(manifest_checklist)):
        raise ValueError('exact CHM manifest byte inventory differs')
    results = []
    for path, expected in sorted(manifest_checklist.items()):
        raw = raw_by_path[path]
        if (not isinstance(path, str) or not SOURCE.fullmatch(path) or not isinstance(expected, str)
                or not re.fullmatch(r'[0-9a-f]{64}', expected) or not isinstance(raw, bytes)
                or not raw or len(raw) > chm.READER_CHM_MANIFEST_BYTES
                or hashlib.sha256(raw).hexdigest() != expected):
            raise ValueError('CHM manifest byte identity differs')
        try:
            manifest = json.loads(raw)
            chm.verify_reader_chapter_limits(raw, manifest, path=path)
        except (TypeError, ValueError) as error:
            raise ValueError('selected CHM manifest fails Reader limits') from error
        results.append({'manifest': path, 'sha256': expected, 'bytes': len(raw)})
    return {'manifests': results, 'remote_mutations': 0, 'deletion_authorized': False}
