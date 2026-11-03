"""Offline-only classification of interrupted CHM subset deletion intents.

This reads no storage and produces no receipt, write, delete or authorization.
Its supplied observations can be stale, forged or incomplete. A pending batch
may be recorded absent only after separate durable ownership, bucket-health,
fresh four-bucket/consumer barriers and per-object readback are established.
"""
try:
    from . import chm_subset_retirement as subset
except ImportError:
    import chm_subset_retirement as subset


def analyze(rows, old_batches, selected, withheld, state, observation):
    """Bind every observed target to one pending cursor, then report next review.

    ``ready-for-independent-verification`` is descriptive only: it is never a
    receipt or permission to persist one. Partial outcomes do not identify who
    deleted a path, and no result in this function authorizes a retry.
    """
    partition = subset.verify_exact_partition(rows, old_batches, selected, withheld)
    cursor = subset.verify_subset_cursor(selected, withheld, state)
    if (not isinstance(observation, dict)
            or observation.get('kind') != 'read-only-exact-chm-subset-reconciliation'
            or type(observation.get('remote_mutations')) is not int
            or observation['remote_mutations'] != 0 or observation.get('deletion_authorized') is not False
            or observation.get('partition') != partition or not isinstance(observation.get('results'), list)):
        raise ValueError('invalid or mismatched reconciliation observation')
    expected = {row['path']: row for row in rows}
    old_absent = {path for batch in old_batches for path in batch['paths']}
    receipt_absent = {path for batch in state['receipts'] for path in batch['paths']}
    pending = set((state.get('pending') or {}).get('paths', []))
    retained = {row['path'] for row in withheld}
    observed, statuses = {}, {}
    for item in observation['results']:
        if (not isinstance(item, dict) or set(item) != {'bucket', 'path', 'bytes', 'sha256', 'status'}
                or not isinstance(item.get('path'), str) or item['path'] not in expected
                or item['path'] in observed
                or {field: item[field] for field in ('bucket', 'path', 'bytes', 'sha256')} != expected[item['path']]):
            raise ValueError('reconciliation target is foreign, duplicated or changed')
        path, status = item['path'], item['status']
        allowed = ({'old-verified-absent'} if path in old_absent else
                   {'subset-verified-absent'} if path in receipt_absent else
                   {'withheld-present'} if path in retained else
                   {'pending-absent', 'pending-present'} if path in pending else {'unsubmitted-present'})
        if status not in allowed:
            raise ValueError('reconciliation status differs from cursor or partition')
        observed[path] = status
        counts = statuses.setdefault(status, {'objects': 0, 'bytes': 0})
        counts['objects'] += 1
        counts['bytes'] += item['bytes']
    if set(observed) != set(expected) or observation.get('counts') != statuses:
        raise ValueError('reconciliation observation is incomplete or totals differ')
    if not pending:
        status = 'subset-already-marked-complete' if cursor['subset_complete'] else 'no-pending-intent'
    elif all(observed[path] == 'pending-absent' for path in pending):
        status = 'ready-for-independent-verification'
    else:
        status = 'pending-unresolved'
    return {'kind': 'offline-chm-subset-recovery-analysis', 'status': status,
            'pending_paths': sorted(pending),
            'pending_present': sorted(path for path in pending if observed[path] == 'pending-present'),
            'pending_absent': sorted(path for path in pending if observed[path] == 'pending-absent'),
            'subset_complete': cursor['subset_complete'], 'original_complete': False,
            'producer_restore_authorized': False, 'remote_mutations': 0, 'deletion_authorized': False,
            'caveats': ['input consistency only; observation provenance and freshness not authenticated',
                        'never writes a receipt or authorizes a delete, retry, or producer restoration']}
