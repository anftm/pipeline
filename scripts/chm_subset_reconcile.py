"""Read-only, exact reconciliation for a potential CHM subset continuation.

The caller supplies a durable-intent snapshot and an accessor returning bytes
or None. Absence must come from an independently reachable bucket, not a
bucket-level 404. This module cannot delete, persist, or authorize any action.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib

try:
    from . import chm_subset_retirement as subset
except ImportError:
    import chm_subset_retirement as subset


def reconcile(rows, old_batches, selected, withheld, state, read_exact, *, workers=4):
    """GET every original target, then classify its relation to durable intent."""
    partition = subset.verify_exact_partition(rows, old_batches, selected, withheld)
    cursor = subset.verify_subset_cursor(selected, withheld, state)
    if not callable(read_exact) or type(workers) is not int or not 1 <= workers <= 16:
        raise ValueError('exact CHM reader or worker count is invalid')
    old_absent = {path for batch in old_batches for path in batch['paths']}
    verified_absent = {path for batch in state['receipts'] for path in batch['paths']}
    pending = set((state.get('pending') or {}).get('paths', []))
    retained = {row['path'] for row in withheld}

    def check(row):
        raw = read_exact(row['bucket'], row['path'])
        if raw is not None and (not isinstance(raw, bytes) or len(raw) != row['bytes']
                                or hashlib.sha256(raw).hexdigest() != row['sha256']):
            raise ValueError('CHM exact target changed: ' + row['path'])
        path = row['path']
        if path in old_absent:
            if raw is not None:
                raise ValueError('original verified-absent target reappeared: ' + path)
            return 'old-verified-absent'
        if path in verified_absent:
            if raw is not None:
                raise ValueError('subset verified-absent target reappeared: ' + path)
            return 'subset-verified-absent'
        if path in retained:
            if raw is None:
                raise ValueError('withheld target is missing: ' + path)
            return 'withheld-present'
        if path in pending:
            return 'pending-absent' if raw is None else 'pending-present'
        if raw is None:
            raise ValueError('unsubmitted subset target is missing: ' + path)
        return 'unsubmitted-present'

    with ThreadPoolExecutor(max_workers=workers) as pool:
        statuses = list(pool.map(check, rows))
    results = [{'bucket': row['bucket'], 'path': row['path'], 'bytes': row['bytes'],
                'sha256': row['sha256'], 'status': status} for row, status in zip(rows, statuses)]
    counts = {}
    for entry in results:
        values = counts.setdefault(entry['status'], {'objects': 0, 'bytes': 0})
        values['objects'] += 1
        values['bytes'] += entry['bytes']
    if cursor['subset_complete'] and (counts.get('pending-absent') or counts.get('pending-present')
                                      or counts.get('unsubmitted-present')):
        raise ValueError('completed subset is not entirely verified absent')
    return {'kind': 'read-only-exact-chm-subset-reconciliation', 'partition': partition,
            'counts': counts, 'results': results, 'remote_mutations': 0, 'deletion_authorized': False}
