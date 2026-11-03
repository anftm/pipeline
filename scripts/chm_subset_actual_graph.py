"""Capture and compare an already-built actual CHM subset reference graph.

This is a validation building block, not a graph scanner, lock, deletion
entrypoint, or authorization. Callers must independently hold all applicable
workflow locks, fence all producers, authenticate their storage reads and
verify both deployed consumers. A fabricated traversal is not evidence.
"""
import copy

try:
    from . import chm_subset_isolated_root as isolated, chm_subset_journal as journal
    from . import chm_subset_retirement as subset
    from . import reader_gc_graph as graph
except ImportError:
    import chm_subset_isolated_root as isolated
    import chm_subset_journal as journal
    import chm_subset_retirement as subset
    import reader_gc_graph as graph


def _check_receipts(selected, withheld, receipts):
    if (not isinstance(receipts, (tuple, list)) or any(
            not isinstance(batch, dict) or set(batch) != {'bucket', 'paths', 'status'} for batch in receipts)):
        raise ValueError('subset receipts are invalid')
    state = {'receipts': list(receipts), 'pending': None, 'subset_complete': False,
             'original_complete': False, 'producer_restore_authorized': False}
    return subset.verify_subset_cursor(selected, withheld, state)


def _check_original_receipts(identity, old_batches):
    if not isinstance(identity, dict) or identity.get('old_receipts_sha256') != journal.digest(old_batches):
        raise ValueError('original receipt identity differs from supplied batches')


def capture(traversal, report, rows, old_batches, selected, withheld, *,
            root_raw, identity, subset_receipts=(), root_rechecked=False, consumers_checked=False):
    """Capture exact membership, protected targets and full graph SHA closure.

    Supplied booleans and in-memory data are not proof that external checks
    actually happened. The result explicitly denies deletion authorization.
    """
    partition = subset.verify_exact_partition(rows, old_batches, selected, withheld)
    _check_original_receipts(identity, old_batches)
    cursor = _check_receipts(selected, withheld, subset_receipts)
    if (not isinstance(report, dict) or report.get('graph_complete') is not True
            or report.get('blockers') != [] or report.get('missing') != []
            or report.get('ambiguous_paths') != [] or report.get('deletion_enabled') is not False
            or set(traversal.files) != set(graph.BUCKETS)
            or set(report.get('buckets', {})) != set(graph.BUCKETS)):
        raise ValueError('actual subset graph is incomplete')
    if type(root_rechecked) is not bool or type(consumers_checked) is not bool:
        raise ValueError('graph recheck flags must be boolean')
    if any(report['buckets'][bucket].get('objects') != len(traversal.files[bucket]) for bucket in graph.BUCKETS):
        raise ValueError('actual subset bucket inventory counts differ')
    candidate = isolated.build(withheld, identity)
    if not isinstance(root_raw, bytes) or root_raw != candidate['raw']:
        raise ValueError('actual subset protection root bytes differ')
    root = (subset.ASSETS, candidate['path'])
    all_refs = {(row['bucket'], row['path']) for row in rows}
    expected_absent = {(batch['bucket'], path) for batch in old_batches for path in batch['paths']}
    expected_absent.update((batch['bucket'], path) for batch in subset_receipts for path in batch['paths'])
    currently_missing = {(row['bucket'], row['path']) for row in rows
                         if row['path'] not in traversal.files[row['bucket']]}
    if currently_missing != expected_absent:
        raise ValueError('actual subset targets differ from original or subset receipts')
    retained = all_refs & traversal.references
    expected_retained = {(row['bucket'], row['path']) for row in withheld}
    if retained != expected_retained:
        raise ValueError('actual subset graph does not protect exactly the withheld targets')
    members = {(bucket, path) for bucket, files in traversal.files.items()
               for path in files if graph.is_root(path, bucket)}
    roots = traversal.root_hashes
    closure = traversal.loaded_digests
    retired = traversal.retired_roots
    if (root not in members or root in retired or roots.get(root) != candidate['sha256']
            or closure.get(root) != candidate['sha256'] or not (members - retired) <= set(roots)
            or not set(roots) <= members or any(closure.get(ref) != digest for ref, digest in roots.items())):
        raise ValueError('actual subset graph root or closure differs')
    return {'kind': 'actual-chm-subset-graph-observation', 'mode': 'actual',
            'graph_complete': True, 'blockers': [], 'ambiguous_paths': [],
            'target_count': len(rows), 'retained_targets': len(retained),
            'subset_receipt_objects': cursor['verified'],
            'subset_receipts': copy.deepcopy(list(subset_receipts)),
            'subset_receipts_sha256': journal.digest(list(subset_receipts)),
            'retained_target_paths': [{'bucket': bucket, 'path': path} for bucket, path in sorted(retained)],
            'root_members': [{'bucket': bucket, 'path': path} for bucket, path in sorted(members)],
            'root_sha256': [{'bucket': bucket, 'path': path, 'sha256': digest}
                            for (bucket, path), digest in sorted(roots.items())],
            'retired_roots': [{'bucket': bucket, 'path': path} for bucket, path in sorted(retired)],
            'closure_sha256': [{'bucket': bucket, 'path': path, 'sha256': digest}
                               for (bucket, path), digest in sorted(closure.items())],
            'root_recheck_complete': root_rechecked, 'reader_acceptance_complete': consumers_checked,
            'partition': partition, 'remote_mutations': 0, 'deletion_authorized': False}


def compare_stage(previous, current, rows, old_batches, selected, withheld, identity, root_raw):
    """Require the complete active root and closure identities to remain fixed.

    This compares supplied observations, not the underlying storage. Receipt
    histories must be exact append-only prefixes of the selected target order;
    callers must independently authenticate receipts and target readbacks. No
    comparison authorizes another deletion.
    """
    partition = subset.verify_exact_partition(rows, old_batches, selected, withheld)
    _check_original_receipts(identity, old_batches)
    candidate = isolated.build(withheld, identity)
    root = (subset.ASSETS, candidate['path'])
    if not isinstance(root_raw, bytes) or root_raw != candidate['raw']:
        raise ValueError('stage protection root bytes differ')

    def refs(proof, field, with_hash=False):
        values = proof.get(field)
        if not isinstance(values, list):
            raise ValueError('stage graph field is missing: ' + field)
        result = {}
        for item in values:
            if (not isinstance(item, dict) or item.get('bucket') not in graph.BUCKETS
                    or not isinstance(item.get('path'), str) or not item['path']):
                raise ValueError('stage graph identity is invalid: ' + field)
            key = (item['bucket'], item['path'])
            if key in result:
                raise ValueError('stage graph identity is duplicated: ' + field)
            if with_hash:
                digest = item.get('sha256')
                if not isinstance(digest, str) or not isolated.SHA.fullmatch(digest):
                    raise ValueError('stage graph SHA is invalid: ' + field)
                result[key] = digest
            else:
                result[key] = True
        return result

    for proof in (previous, current):
        if (not isinstance(proof, dict) or proof.get('kind') != 'actual-chm-subset-graph-observation'
                or proof.get('mode') != 'actual' or proof.get('graph_complete') is not True
                or proof.get('blockers') != [] or proof.get('ambiguous_paths') != []
                or proof.get('root_recheck_complete') is not True
                or proof.get('reader_acceptance_complete') is not True
                or type(proof.get('remote_mutations')) is not int or proof['remote_mutations'] != 0
                or proof.get('deletion_authorized') is not False
                or type(proof.get('subset_receipt_objects')) is not int or proof['subset_receipt_objects'] < 0):
            raise ValueError('stage graph is incomplete or claims authority')
        receipts = proof.get('subset_receipts')
        cursor = _check_receipts(selected, withheld, receipts)
        if (proof['subset_receipt_objects'] != cursor['verified']
                or proof.get('subset_receipts_sha256') != journal.digest(receipts)
                or proof.get('target_count') != len(rows) or proof.get('partition') != partition):
            raise ValueError('stage graph receipt history or exact partition differs')
        members, retired = refs(proof, 'root_members'), refs(proof, 'retired_roots')
        roots, closure = refs(proof, 'root_sha256', True), refs(proof, 'closure_sha256', True)
        expected_retained = {(row['bucket'], row['path']) for row in withheld}
        if (not members or not set(retired) <= set(members) or not (set(members) - set(retired)) <= set(roots)
                or not set(roots) <= set(members) or any(closure.get(ref) != digest for ref, digest in roots.items())
                or root in retired or roots.get(root) != candidate['sha256'] or closure.get(root) != candidate['sha256']
                or set(refs(proof, 'retained_target_paths')) != expected_retained
                or proof.get('retained_targets') != len(expected_retained)):
            raise ValueError('stage graph protection, roots or closure are inconsistent')
    previous_receipts = previous['subset_receipts']
    current_receipts = current['subset_receipts']
    if (len(previous_receipts) > len(current_receipts)
            or current_receipts[:len(previous_receipts)] != previous_receipts
            or previous['subset_receipt_objects'] > current['subset_receipt_objects']
            or refs(previous, 'root_members') != refs(current, 'root_members')
            or refs(previous, 'retired_roots') != refs(current, 'retired_roots')
            or refs(previous, 'root_sha256', True) != refs(current, 'root_sha256', True)
            or refs(previous, 'closure_sha256', True) != refs(current, 'closure_sha256', True)):
        raise ValueError('stage graph changed or receipt history regressed or was rewritten')
    return {'kind': 'actual-chm-subset-stage-comparison', 'root_sha256': candidate['sha256'],
            'previous_receipts': previous['subset_receipt_objects'],
            'current_receipts': current['subset_receipt_objects'],
            'remote_mutations': 0, 'deletion_authorized': False}


def compare_original(previous, actual, withheld, identity, root_raw):
    """Require the single independent root as the only closure addition.

    Reuses the exact, fail-closed diagnostic comparator. Reinterpreting a copy
    for comparison never changes the real proof's mode or grants authority.
    """
    if (not isinstance(actual, dict) or actual.get('kind') != 'actual-chm-subset-graph-observation'
            or actual.get('mode') != 'actual' or actual.get('deletion_authorized') is not False
            or actual.get('root_recheck_complete') is not True
            or actual.get('reader_acceptance_complete') is not True):
        raise ValueError('actual subset graph rechecks are incomplete')
    candidate = isolated.build(withheld, identity)
    result = isolated.compare(previous, {**actual, 'mode': 'hypothetical-overlay'},
                              isolated_path=candidate['path'], isolated_raw=root_raw,
                              withheld=withheld, identity=identity)
    return {**result, 'mode': 'actual-graph-comparison-only',
            'deletion_authorized': False, 'remote_mutations': 0}
