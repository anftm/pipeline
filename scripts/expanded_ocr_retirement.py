#!/usr/bin/env python3
"""Plan and verify a bounded OCR withdrawal.

Mutation helpers are deliberately separate from the read-only preview. An
execution entry point must supply durable checkpoints, a producer fence, an
actual post-retirement graph and the exact reviewed inventory before using them.
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
FIRST_PROCESSING = 'reader-index/processing/ccrd-body-rebuild-d0d56b80639bf808-20261025.json'
SECOND_PROCESSING = 'reader-index/processing/ccrd-body-rebuild-fb1abf531f3a630d-20261025.json'
PROCESSING_HASHES = {
    FIRST_PROCESSING: '38c54d4d99abcf516c0b199cb32a702b0b75749d3c0757a32371f43d2635aa05',
    SECOND_PROCESSING: '33a94ca27d53b9a7737f1d53e7932d75096c5752d3ce0986acba59448dbfc048',
}
PRESERVED_DERIVED_ROOT = 'derived/migration-20261007/6f9f9f251a929f25e1b5be3beb32ec60'
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
    # Scheduled maintenance may legitimately refresh only the consumer
    # receipt. Re-read and re-verify it rather than pinning an obsolete digest;
    # create_plan still verifies the exact release and snapshots the fresh SHA.
    prior = exact.create_plan(store, client, exact.load_targets(), lambda _: None,
                              expected_reader_consumer_sha=None)
    targets = {(row['bucket'], row['path']) for row in rows}
    external = prior['updates']['external']['resources']
    kept = [ref for ref in external if (ref['bucket'], ref['path']) not in targets]
    if (len(external) != 2531 or len(kept) != 1 or kept[0]['bucket'] != PAGES
            or kept[0]['path'] != 'derived/migration-20261007/6f9f9f251a929f25e1b5be3beb32ec60/derived-manifest.json'):
        raise ValueError('outside-scope provenance differs from reviewed current-reader source')
    plan = copy.deepcopy(prior)
    plan['updates']['external']['resources'] = kept
    plan['updates']['withdrawal']['preserved_reader_provenance']['resources'] = len(kept)
    plan['updates']['withdrawal']['retired_external_resources'] = len(external) - len(kept)
    lifecycle.references(plan['updates']['external'], PRESERVED)
    plan['expanded_targets'] = copy.deepcopy(rows)
    plan['inventory_sha256'] = INVENTORY_SHA256
    plan['preserved_external_resources'] = len(kept)
    plan['retired_ocr_or_ps_external_resources'] = len(external) - len(kept)
    plan['processing_updates'] = prepare_processing_updates(store, rows)
    return plan


def prepare_processing_updates(store, rows):
    """Retire only exact OCR upload protections, retaining any non-target trees."""
    target_paths = {}
    for row in rows:
        target_paths.setdefault(row['bucket'], set()).add(row['path'])
    old = {}
    for path, expected in PROCESSING_HASHES.items():
        raw = store.read_bytes(ASSETS, path)
        if sha(raw) != expected:
            raise ValueError('OCR processing protection changed: ' + path)
        value = json.loads(raw)
        if (value.get('version') != 1 or value.get('kind') != 'reader-processing-roots'
                or value.get('operation') != 'ccrd-body-evidence-upload'
                or value.get('status') != 'uploaded'
                or value.get('release_policy') != 'explicit-publisher-handoff'
                or not isinstance(value.get('resources'), list)):
            raise ValueError('unexpected OCR processing protection schema')
        old[path] = value
    first, second = old[FIRST_PROCESSING], old[SECOND_PROCESSING]
    if (second.get('candidate_generation') != exact.GENERATION
            or second.get('previous_protection') != FIRST_PROCESSING
            or second.get('previous_protection_sha256') != PROCESSING_HASHES[FIRST_PROCESSING]):
        raise ValueError('OCR processing protection chain changed')
    result = {}
    kept_roots = []
    for path, value in old.items():
        keep, removed = [], []
        for ref in value['resources']:
            if (not isinstance(ref, dict) or ref.get('bucket') not in {ASSETS, PAGES, INPUT}
                    or not isinstance(ref.get('root'), str)):
                raise ValueError('invalid OCR processing root')
            bucket, root = ref['bucket'], ref['root']
            # A root is retained whole when even one object in it is not part of
            # the approved deletion list; never narrow a mixed shared tree.
            files = store.list_files(bucket, (root + '/',))
            target = {p for p in target_paths.get(bucket, set()) if p.startswith(root + '/')}
            if target and not target <= files:
                raise ValueError('an OCR target is absent from its protected tree')
            if target and files == target:
                removed.append(copy.deepcopy(ref))
            else:
                keep.append(copy.deepcopy(ref))
        if not removed:
            raise ValueError('OCR processing record has no exact retired resources')
        update = copy.deepcopy(value)
        update['resources'] = keep
        update['withdrawal'] = {'kind': 'explicit-ocr-withdrawal', 'inventory_sha256': INVENTORY_SHA256,
                                'removed_root_count': len(removed), 'original_sha256': PROCESSING_HASHES[path]}
        if not keep:
            # No current generic code understands a special terminal status for
            # an empty processing record. Keep the schema but no retained roots;
            # the audit describes why its protection is empty.
            update['status'] = 'withdrawn'
        kept_roots.extend(keep)
        # Preserve exact original wire bytes, not a reserialized object: this
        # binds the recovery plan to the reviewed SHA and supports safe rollback.
        raw = store.read_bytes(ASSETS, path)
        if sha(raw) != PROCESSING_HASHES[path] or json.loads(raw) != value:
            raise ValueError('processing root changed while planning: ' + path)
        result[path] = {'sha256_before': PROCESSING_HASHES[path], 'before': value,
                        'before_raw': raw.decode('utf-8'), 'after': update}
    if kept_roots != [{'bucket': PAGES, 'root': PRESERVED_DERIVED_ROOT}]:
        raise ValueError('unexpected mixed or non-target OCR processing tree')
    return result


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


def verify_exact_targets(store, rows, *, workers=8):
    """Freshly compare every approved object's length and SHA, never infer from paths."""
    from concurrent.futures import ThreadPoolExecutor

    def check(row):
        raw = store.read_bytes(row['bucket'], row['path'])
        if len(raw) != row['bytes'] or sha(raw) != row['sha256']:
            raise ValueError('approved target changed: ' + row['bucket'] + ':' + row['path'])
        return len(raw)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        total = sum(pool.map(check, rows))
    if total != sum(row['bytes'] for row in rows):
        raise ValueError('approved target byte total changed')
    return total


def assert_same_inventory(plan, rows):
    if (plan.get('inventory_sha256') != INVENTORY_SHA256 or plan.get('expanded_targets') != rows
            or plan.get('generation') != exact.GENERATION or plan.get('version') != 1
            or plan.get('kind') != 'ccrd-exact-ocr-retirement-plan'):
        raise ValueError('saved plan does not match the exact approved inventory')
    approved_ccrd = {(row['path'], row['bytes'], row['sha256']) for row in rows if row['bucket'] == CCRD}
    if approved_ccrd != {(r['path'], r['bytes'], r['sha256']) for r in plan.get('targets', [])}:
        raise ValueError('saved CCRD targets differ from the approved inventory')
    if any(plan.get('control_hashes_before', {}).get(path) != digest
           for path, digest in exact.INITIAL_HASHES.items()):
        raise ValueError('saved CCRD controls are not the reviewed originals')
    if set(plan.get('processing_updates', {})) != set(PROCESSING_HASHES):
        raise ValueError('saved plan does not contain exactly the reviewed processing roots')
    for path, change in plan['processing_updates'].items():
        before, after = change.get('before'), change.get('after')
        if (not isinstance(before, dict) or not isinstance(after, dict)
                or change.get('sha256_before') != PROCESSING_HASHES[path]
                or not isinstance(change.get('before_raw'), str)
                or sha(change['before_raw'].encode('utf-8')) != change['sha256_before']
                or json.loads(change['before_raw']) != before):
            raise ValueError('saved processing root is not the reviewed original')
        original = before.get('resources')
        if not isinstance(original, list):
            raise ValueError('invalid saved processing resources')
        allowed_retained = ([{'bucket': PAGES, 'root': PRESERVED_DERIVED_ROOT}]
                            if path == FIRST_PROCESSING else [])
        expected_after = copy.deepcopy(before)
        expected_after['resources'] = allowed_retained
        expected_after['withdrawal'] = {'kind': 'explicit-ocr-withdrawal',
            'inventory_sha256': INVENTORY_SHA256, 'removed_root_count': len(original) - len(allowed_retained),
            'original_sha256': PROCESSING_HASHES[path]}
        if not allowed_retained:
            expected_after['status'] = 'withdrawn'
        if after != expected_after or any(ref not in original for ref in allowed_retained):
            raise ValueError('saved processing update exceeds the reviewed withdrawal')
        for ref in original:
            if ref in allowed_retained:
                continue
            if not isinstance(ref, dict) or not any(row['bucket'] == ref.get('bucket')
                    and row['path'].startswith(str(ref.get('root', '')) + '/') for row in rows):
                raise ValueError('saved processing update removes a foreign root')
    updates = plan.get('updates', {})
    external = updates.get('external', {}).get('resources')
    if (not isinstance(external, list) or len(external) != 1 or not isinstance(external[0], dict)
            or external[0].get('bucket') != PAGES
            or external[0].get('path') != PRESERVED_DERIVED_ROOT + '/derived-manifest.json'
            or updates.get('intent', {}).get('status') != 'retired' or updates['intent'].get('resources') != []
            or updates.get('ids', {}).get('status') != 'withdrawn' or updates['ids'].get('resources') != []):
        raise ValueError('saved CCRD retirement is not narrowly scoped')
    target = {(row['bucket'], row['path']) for row in rows}
    retained = updates.get('reader_consumers', {}).get('resources')
    if not isinstance(retained, list) or any((ref.get('bucket'), ref.get('path')) in target for ref in retained):
        raise ValueError('saved Reader consumer still pins an OCR retirement target')


def verify_saved_plan_controls(store, plan):
    """Prove proposed post-state is derived from each exact reviewed root."""
    changes = ((CCRD, exact.INTENT, plan['updates']['intent'], 'intent'),
               (CCRD, exact.IDS, plan['updates']['ids'], 'ids'))
    for bucket, path, replacement, kind in changes:
        raw = store.read_bytes(bucket, path)
        if sha(raw) == plan['control_hashes_before'][path]:
            expected = json.loads(raw)
            if kind == 'intent':
                expected.update(status='retired', resources=[], retirement_reason='explicit-document-reset',
                                reset_id=exact.RESET_ID)
            else:
                expected.update(status='withdrawn', resources=[], reset_id=exact.RESET_ID)
            if expected != replacement:
                raise ValueError('saved replacement changes unrelated CCRD fields: ' + path)
        elif raw != encode(replacement):
            raise ValueError('CCRD root is neither the reviewed original nor replacement: ' + path)
    raw = store.read_bytes(ASSETS, exact.READER_CONSUMERS)
    proposed = plan['updates']['reader_consumers']
    if sha(raw) == plan['reader_consumer_sha256_before']:
        before = json.loads(raw)
        expected = copy.deepcopy(before)
        release = exact.identity(plan['pointer_before']['release'])
        expected['resources'] = [ref for ref in before['resources'] if exact.identity(ref) != release]
        expected['receipts']['ccrd'] = copy.deepcopy(proposed['receipts']['ccrd'])
        if expected != proposed or len(before['resources']) != len(expected['resources']) + 1:
            raise ValueError('saved Reader consumer update removes unrelated resources')
    elif raw != encode(proposed):
        raise ValueError('Reader consumer is neither the reviewed original nor replacement')


def apply_planned_reference_retirement(store, plan, fence, client, checkpoint):
    """Apply the bounded reference changes, without deleting target data."""
    fence.check()
    exact.current_status(client, plan['base_generation'])
    for path, change in plan['processing_updates'].items():
        observed = store.read_bytes(ASSETS, path)
        if sha(observed) != change['sha256_before'] and observed != encode(change['after']):
            raise ValueError('OCR upload protection changed before retirement: ' + path)
    exact.apply_reference_retirement(store, plan, fence, client, checkpoint)
    for path, change in sorted(plan['processing_updates'].items()):
        fence.check()
        exact.ensure_bytes(store, ASSETS, path, change['sha256_before'], encode(change['after']))
        checkpoint('ocr-upload-protection-retired')
    exact.verify_reference_retirement(store, plan)


def require_actual_zero_proof(proof, rows):
    if (proof.get('graph_complete') is not True or proof.get('actual_retired_controls_checked') is not True
            or proof.get('internal_target_manifests_omitted') is not True
            or proof.get('target_exclusivity_proven') is not True
            or proof.get('retained_target_count') != 0 or proof.get('non_target_origins')
            or proof.get('tree_roots_without_origin') != 0
            or proof.get('target_count') != len(rows)):
        raise ValueError('current actual four-bucket closure does not authorize exact retirement')


def check_target_identity(store, row):
    """Return present/absent only after rejecting transport and bucket failures."""
    raw = exact.read_optional(store, row['bucket'], row['path'])
    if raw is None:
        return False
    if len(raw) != row['bytes'] or sha(raw) != row['sha256']:
        raise ValueError('approved target changed: ' + row['bucket'] + ':' + row['path'])
    return True


def submitted_targets(progress, rows):
    """A missing object is recoverable only if a durable record submitted it."""
    keys = {(row['bucket'], row['path']) for row in rows}
    submitted = set()
    for batch in progress.get('batches', []):
        paths = batch.get('paths')
        if (batch.get('status') not in {'submitted', 'verified-absent'} or not isinstance(paths, list)
                or not paths or len(paths) > 100 or len(paths) != len(set(paths))):
            raise ValueError('invalid saved deletion batch')
        entries = {(batch.get('bucket'), path) for path in paths}
        if not entries <= keys or entries & submitted:
            raise ValueError('saved deletion batches overlap or exceed the approved inventory')
        submitted.update(entries)
    return submitted


def reconcile_targets(store, rows, progress, *, workers=8):
    """Individually re-read targets and account for interrupted batch outcomes."""
    from concurrent.futures import ThreadPoolExecutor

    submitted = submitted_targets(progress, rows)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        present = list(pool.map(lambda row: check_target_identity(store, row), rows))
    missing = {(row['bucket'], row['path']) for row, exists in zip(rows, present) if not exists}
    if not missing <= submitted:
        raise ValueError('an approved target disappeared without a durable deletion submission')
    return {'present': sum(present), 'confirmed_absent': len(missing),
            'confirmed_absent_bytes': sum(row['bytes'] for row, exists in zip(rows, present) if not exists),
            'missing': missing}


def delete_approved_batches(store, rows, fence, client, checkpoint, *, batch_size=100, progress=None):
    """Submit only reviewed identities; reconcile per-key results after failures."""
    if not 1 <= batch_size <= 100:
        raise ValueError('invalid exact deletion batch size')
    result = copy.deepcopy(progress or {'batches': []})
    state = reconcile_targets(store, rows, result)
    result.update(confirmed_deleted_objects=state['confirmed_absent'],
                  confirmed_deleted_bytes=state['confirmed_absent_bytes'])
    checkpoint('deletion-reconciled', result)
    order = (INPUT, PAGES, ASSETS, CCRD)
    authorized = {(r['bucket'], r['path']): r for r in rows}
    for bucket in order:
        # Previously submitted batches are retried in place. Missing keys are
        # harmless only because they were already included in that durable batch.
        batches = [batch for batch in result['batches'] if batch['bucket'] == bucket]
        submitted = submitted_targets(result, rows)
        unsubmitted = [r for r in rows if r['bucket'] == bucket and (bucket, r['path']) not in submitted]
        batches.extend({'bucket': bucket, 'paths': [r['path'] for r in unsubmitted[i:i + batch_size]],
                        'status': 'new'} for i in range(0, len(unsubmitted), batch_size))
        for receipt in batches:
            fence.check()
            batch = [authorized[(bucket, path)] for path in receipt['paths']]
            existing = [row for row in batch if check_target_identity(store, row)]
            if receipt['status'] == 'new':
                if len(existing) != len(batch):
                    raise ValueError('unsubmitted target disappeared before deletion')
                receipt['status'] = 'submitted'
                result['batches'].append(receipt)
            checkpoint('deletion-batch-submitted', result)
            if existing:
                store.delete(bucket, [row['path'] for row in existing])
            for row in batch:
                if exact.read_optional(store, bucket, row['path']) is not None:
                    raise ValueError('target remains directly readable: ' + bucket + ':' + row['path'])
            receipt['status'] = 'verified-absent'
            verified = {(entry['bucket'], path) for entry in result['batches']
                        if entry['status'] == 'verified-absent' for path in entry['paths']}
            verified.update(state['missing'])
            result['confirmed_deleted_objects'] = len(verified)
            result['confirmed_deleted_bytes'] = sum(authorized[key]['bytes'] for key in verified)
            checkpoint('deletion-batch-verified', result)
    verified = reconcile_targets(store, rows, result)
    if verified['present']:
        raise ValueError('approved targets still exist after deletion')
    result.update(confirmed_deleted_objects=verified['confirmed_absent'],
                  confirmed_deleted_bytes=verified['confirmed_absent_bytes'])
    for row in rows:
        if row['bucket'] == CCRD and row['path'].endswith('.txt'):
            response = client.get('https://huggingface.co/buckets/' + CCRD + '/resolve/' + row['path'],
                                  headers={'Range': 'bytes=0-0', 'Cache-Control': 'no-cache'}, timeout=60)
            if response.status_code not in {404, 410}:
                raise ValueError('public OCR TXT remains accessible: ' + row['path'])
    if result['confirmed_deleted_objects'] != len(rows):
        raise ValueError('not all approved objects have been verified absent')
    return result


def recheck_roots_and_controls(store, plan, proof, fence, client):
    """Re-list roots and verify their exact read-time bytes before first deletion."""
    from concurrent.futures import ThreadPoolExecutor

    require_actual_zero_proof(proof, plan['expanded_targets'])
    expected = {(r['bucket'], r['path']) for r in proof['root_members']}
    expected_hashes = {(r['bucket'], r['path']): r['sha256'] for r in proof['root_sha256']}
    retired = {(r['bucket'], r['path']) for r in proof['retired_root_members']}
    if not (expected - retired) <= set(expected_hashes):
        raise ValueError('root fingerprint evidence is incomplete')
    observed = set()
    for bucket in graph.BUCKETS:
        if bucket == CCRD:
            roots = {p for p in store.list_files(bucket, ('control/', 'current.json'))
                     if graph.is_root(p, bucket)}
        else:
            parents = {posixpath.dirname(path) + '/' for owner, path in expected
                       if owner == bucket and not path.startswith('reader-index/') and '/' in path}
            roots = {p for p in store.list_roots(bucket, parents) if graph.is_root(p, bucket)}
        observed.update((bucket, path) for path in roots)
    if observed != expected:
        raise ValueError('root inventory changed before exact deletion')
    def read_hash(item):
        ref, expected_digest = item
        if store.digest(*ref) != expected_digest:
            raise ValueError('root changed before exact deletion: ' + ':'.join(ref))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(read_hash, sorted(expected_hashes.items())))
    exact.verify_reference_retirement(store, plan)
    for path, change in plan['processing_updates'].items():
        if store.read_bytes(ASSETS, path) != encode(change['after']):
            raise ValueError('OCR processing protection changed before deletion: ' + path)
    fence.check()
    exact.current_status(client, plan['base_generation'])


def check_partial_controls(store, plan):
    """Only exact original or planned control bytes are valid during recovery."""
    controls, _ = exact.inspect_roots(store, None, require_initial=False)
    expected = dict(plan['control_hashes_before'])
    allowed_paths = set(expected) | {exact.EXTERNAL}
    if set(controls) - allowed_paths or set(expected) - set(controls) - {exact.CURRENT}:
        raise ValueError('CCRD control inventory changed during partial retirement')
    replacements = {exact.INTENT: sha(encode(plan['updates']['intent'])),
                    exact.IDS: sha(encode(plan['updates']['ids'])),
                    exact.EXTERNAL: sha(encode(plan['updates']['external']))}
    for path, actual in controls.items():
        allowed = {expected.get(path), replacements.get(path)} - {None}
        if actual not in allowed:
            raise ValueError('CCRD control changed during partial retirement: ' + path)
    reader_hash = sha(store.read_bytes(ASSETS, exact.READER_CONSUMERS))
    if reader_hash not in {plan['reader_consumer_sha256_before'], sha(encode(plan['updates']['reader_consumers']))}:
        raise ValueError('Reader consumer changed during partial retirement')
    for path, change in plan['processing_updates'].items():
        raw = store.read_bytes(ASSETS, path)
        if sha(raw) != change['sha256_before'] and raw != encode(change['after']):
            raise ValueError('processing root changed during partial retirement: ' + path)
    receipt = exact.read_optional(store, CCRD, exact.WITHDRAWAL)
    if receipt is not None and receipt != encode(plan['updates']['withdrawal']):
        raise ValueError('withdrawal receipt changed during partial retirement')


def simulate(store, plan, fence, *, workers=8, deadline_minutes=300, progress=None,
             omit_internal_target_manifests=False, actual=False, details=None, allow_partial=False):
    """Find remaining target pins without changing a single production object."""
    overrides = {(CCRD, PRESERVED): encode(plan['updates']['external']),
                 (CCRD, exact.INTENT): encode(plan['updates']['intent']),
                 (CCRD, exact.IDS): encode(plan['updates']['ids']),
                 (ASSETS, exact.READER_CONSUMERS): encode(plan['updates']['reader_consumers'])}
    overrides.update({(ASSETS, path): encode(change['after'])
                      for path, change in plan.get('processing_updates', {}).items()})
    hidden = {(CCRD, exact.CURRENT)}
    if actual and allow_partial:
        raise ValueError('actual graph cannot use partial control overlays')
    if allow_partial:
        check_partial_controls(store, plan)
    if actual:
        for (bucket, path), expected in overrides.items():
            if store.read_bytes(bucket, path) != expected:
                raise ValueError('actual retired root differs from the verified plan: ' + bucket + ':' + path)
        if exact.read_optional(store, CCRD, exact.CURRENT) is not None:
            raise ValueError('OCR current pointer still exists')

    class View:
        def __init__(self):
            self.object_sizes = store.object_sizes
            # Actual traversal must expose fresh listed and read-time ETags.
            # The simulated overlay instead uses synthetic hashes for its roots.
            self.read_etags = getattr(store, 'read_etags', {}) if actual else {}
            self.root_etags = getattr(store, 'root_etags', {}) if actual else {}

        def list_files(self, bucket, prefixes):
            files = set(store.list_files(bucket, prefixes))
            if actual:
                return files
            files.difference_update(path for owner, path in hidden if owner == bucket)
            files.update(path for owner, path in overrides if owner == bucket and path.startswith(prefixes))
            return files

        def list_roots(self, bucket, parents):
            roots = {path: etag for path, etag in store.list_roots(bucket, parents).items()
                     if graph.is_root(path, bucket)}
            if actual:
                return roots
            for owner, path in hidden:
                if owner == bucket:
                    roots.pop(path, None)
            for owner, path in overrides:
                if owner == bucket and graph.is_root(path, bucket):
                    roots[path] = None
            return roots

        def read_bytes(self, bucket, path):
            ref = (bucket, path)
            if not actual and ref in hidden:
                raise FileNotFoundError(path)
            if actual:
                return store.read_bytes(bucket, path)
            return overrides[ref] if ref in overrides else store.read_bytes(bucket, path)

        def read_graph_json(self, bucket, path, parser=None):
            ref = (bucket, path)
            if not actual and ref in hidden:
                raise FileNotFoundError(path)
            if actual:
                # Actual destructive proof must read the current bytes, not a
                # cache entry selected by a potentially stale listed ETag.
                if ref == graph.CATALOG_REF:
                    return store.read_graph_json(bucket, path, parser=parser)
                raw = store.read_bytes(bucket, path)
                return (parser or stream.graph_json)([raw], path.endswith('.gz'))
            if ref in overrides:
                return (parser or stream.graph_json)([overrides[ref]], path.endswith('.gz'))
            return store.read_graph_json(bucket, path, parser=parser)

        def digest(self, bucket, path):
            ref = (bucket, path)
            if actual:
                return store.digest(bucket, path)
            return sha(overrides[ref]) if ref in overrides else store.digest(bucket, path)

    fence.check()
    target_set = {(row['bucket'], row['path']) for row in plan['expanded_targets']}
    ref_graph = OriginReferenceGraph(View(), workers=workers, max_rounds=40, progress=progress,
                                     omit_internal=target_set if omit_internal_target_manifests else None,
                                     deadline=time.monotonic() + deadline_minutes * 60)
    report = ref_graph.build()
    if not report['graph_complete'] or report['blockers'] or report.get('ambiguous_paths'):
        raise ValueError('current four-bucket graph incomplete or ambiguous: ' + repr(report['blockers'][:5]))
    if ref_graph.omitted_retired_input_pins:
        raise ValueError('a retired OCR input is referenced by a non-target root: '
                         + repr(sorted(ref_graph.omitted_retired_input_pins)[:5]))
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
    if actual:
        exact.verify_reference_retirement(store, plan)
        for path, update in plan.get('processing_updates', {}).items():
            if store.read_bytes(ASSETS, path) != encode(update['after']):
                raise ValueError('OCR upload protection changed during actual scan: ' + path)
    elif allow_partial:
        check_partial_controls(store, plan)
    else:
        controls, _ = exact.inspect_roots(store, None)
        if controls != plan['control_hashes_before']:
            raise ValueError('CCRD controls changed during reference simulation')
        if sha(store.read_bytes(ASSETS, exact.READER_CONSUMERS)) != plan['reader_consumer_sha256_before']:
            raise ValueError('Reader consumers changed during reference simulation')
        for path, update in plan.get('processing_updates', {}).items():
            if sha(store.read_bytes(ASSETS, path)) != update['sha256_before']:
                raise ValueError('OCR upload protection changed during simulation: ' + path)
    attestation = attest_root_fingerprints(report, ref_graph)
    zero_proof = (zero_root_target_proof(report, ref_graph, plan['expanded_targets'])
                  if omit_internal_target_manifests else None)
    if details is not None:
        details.update(reference_graph=ref_graph, report=report)
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
            'actual_retired_controls_checked': actual,
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
        self.omitted_retired_input_pins = set()

    def load(self, ref):
        if ref in self.omit_internal and not graph.is_root(ref[1], ref[0]):
            # Target bytes were independently hashed in the inventory; a target
            # is treated as a terminal boundary in this hypothetical view.
            self.omitted_manifest_reads.add(ref)
            return None
        return super().load(ref)

    def mark(self, bucket, path, *, tree=False, external_pin=False):
        # The generic collector intentionally ignores older Reader pins to
        # retired OCR inputs. This exact withdrawal cannot assume those pins
        # are stale: record an explicit source even if the base graph skips it.
        ref = (bucket, path)
        if (self.origin is not None and not tree and isinstance(path, str)
                and bucket in graph.BUCKETS and self.input_retired(ref)):
            self.direct.setdefault(self.origin, set()).add(ref)
            if self.origin not in self.omit_internal:
                self.omitted_retired_input_pins.add((self.origin, ref))
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
            elif not isinstance(owner, tuple) or len(owner) != 2:
                raise ValueError('invalid recorded tree owner')
            elif owner not in targets:
                hits.setdefault(owner, set()).update(closure)
    return hits
