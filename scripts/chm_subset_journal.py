"""Immutable, offline-only envelopes for the CHM subset recovery design.

No storage backend or execution entrypoint is provided.  In particular,
``make_envelope`` does not create a durable intent, hold a lock, or authorize a
write or delete. A future backend must preserve these identities and solve S3
conditional-write ownership, unknown results, ABA, and producer fencing first.
"""
import copy
import hashlib
import json
import re

try:
    from . import chm_subset_retirement as subset
except ImportError:
    import chm_subset_retirement as subset


SHA = re.compile(r'[0-9a-f]{64}')
TOKEN = re.compile(r'[0-9a-f]{32}')
KIND = 'chm-subset-cursor-envelope-v1'


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def _sha(value, name):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError('invalid ' + name + ' SHA-256')


def _plan(selected, withheld, identity):
    if not isinstance(identity, dict) or set(identity) != {
            'old_plan_sha256', 'inventory_sha256', 'old_progress_sha256', 'old_receipts_sha256',
            'classification_sha256', 'maintenance_id', 'transaction_id'}:
        raise ValueError('subset journal identity is incomplete')
    for field in ('old_plan_sha256', 'inventory_sha256', 'old_progress_sha256', 'old_receipts_sha256',
                  'classification_sha256'):
        _sha(identity[field], field)
    if any(not isinstance(identity[field], str) or not TOKEN.fullmatch(identity[field])
           for field in ('maintenance_id', 'transaction_id')):
        raise ValueError('invalid subset maintenance or transaction identity')
    if any(identity[field] == '0' * 32 for field in ('maintenance_id', 'transaction_id')):
        raise ValueError('subset maintenance or transaction identity cannot be the all-zero placeholder')
    if any(not isinstance(rows, list) or any(not isinstance(row, dict) or set(row) != {
            'bucket', 'path', 'bytes', 'sha256'} for row in rows) for rows in (selected, withheld)):
        raise ValueError('subset plan contains invalid or extra target fields')
    seen = set()
    for row in [*selected, *withheld]:
        path = row['path']
        if (not isinstance(path, str) or not subset.TARGET.fullmatch(path) or row['bucket'] != subset.ASSETS
                or path in seen or type(row['bytes']) is not int or row['bytes'] < 1
                or not isinstance(row['sha256'], str) or not SHA.fullmatch(row['sha256'])):
            raise ValueError('subset plan contains invalid or duplicate target identity')
        seen.add(path)
    initial = {'receipts': [], 'pending': None, 'subset_complete': False,
               'original_complete': False, 'producer_restore_authorized': False}
    subset.verify_subset_cursor(selected, withheld, initial)
    return {'kind': 'local-only-chm-subset-journal-plan-v1', 'identity': copy.deepcopy(identity),
            'selected': copy.deepcopy(selected), 'withheld': copy.deepcopy(withheld),
            'deletion_authorized': False}


def bind_plan(rows, old_batches, selected, withheld, identity, *, old_progress_raw):
    """Bind the exact original receipts and disjoint inventory into a plan."""
    if not isinstance(identity, dict):
        raise ValueError('invalid subset journal identity')
    if (not isinstance(old_progress_raw, bytes) or not isinstance(identity.get('old_progress_sha256'), str)
            or hashlib.sha256(old_progress_raw).hexdigest() != identity['old_progress_sha256']):
        raise ValueError('original CHM progress byte SHA differs')
    try:
        old_progress = json.loads(old_progress_raw)
    except (TypeError, ValueError) as error:
        raise ValueError('original CHM progress bytes are invalid') from error
    if (not isinstance(old_progress, dict) or old_progress.get('batches') != old_batches
            or old_progress.get('kind') != 'chm-epub-retirement'
            or old_progress.get('plan_sha256') != identity.get('old_plan_sha256')
            or old_progress.get('inventory_sha256') != identity.get('inventory_sha256')
            or old_progress.get('maintenance_id') != identity.get('maintenance_id')
            or old_progress.get('complete') is not False):
        raise ValueError('original CHM progress and subset identity differ')
    partition = subset.verify_exact_partition(rows, old_batches, selected, withheld)
    if (type(old_progress.get('confirmed_deleted_objects')) is not int
            or old_progress['confirmed_deleted_objects'] != partition['old_receipts']
            or type(old_progress.get('confirmed_deleted_bytes')) is not int
            or old_progress['confirmed_deleted_bytes'] != partition['old_receipt_bytes']):
        raise ValueError('original CHM confirmed receipt counts or bytes differ')
    if digest(old_batches) != identity.get('old_receipts_sha256'):
        raise ValueError('original CHM receipt digest differs')
    plan = _plan(selected, withheld, identity)
    return {'plan': plan, 'plan_sha256': digest(plan), 'partition': partition,
            'remote_mutations': 0, 'deletion_authorized': False}


def make_envelope(plan, state, *, sequence, parent_sha256, nonce):
    """Construct (but do not persist) an immutable, plan-bound cursor snapshot."""
    if (not isinstance(plan, dict) or set(plan) != {'kind', 'identity', 'selected', 'withheld',
                                                   'deletion_authorized'}
            or plan.get('kind') != 'local-only-chm-subset-journal-plan-v1'
            or plan.get('deletion_authorized') is not False
            or _plan(plan.get('selected'), plan.get('withheld'), plan.get('identity')) != plan):
        raise ValueError('subset journal plan is invalid')
    if not isinstance(state, dict) or set(state) != {'receipts', 'pending', 'subset_complete',
                                                     'original_complete', 'producer_restore_authorized'}:
        raise ValueError('invalid subset cursor state')
    subset.verify_subset_cursor(plan['selected'], plan['withheld'], state)
    for receipt in state['receipts']:
        if set(receipt) != {'bucket', 'paths', 'status'}:
            raise ValueError('unexpected subset receipt fields')
    if state['pending'] is not None and set(state['pending']) != {'bucket', 'paths', 'status'}:
        raise ValueError('unexpected subset intent fields')
    if (type(sequence) is not int or sequence < 0 or not isinstance(nonce, str) or not TOKEN.fullmatch(nonce)
            or nonce == '0' * 32
            or (sequence == 0 and parent_sha256 is not None)
            or (sequence > 0 and (not isinstance(parent_sha256, str) or not SHA.fullmatch(parent_sha256)))):
        raise ValueError('invalid subset envelope sequence, parent, or nonce')
    if sequence == 0 and (state['pending'] is not None or state['receipts'] or state['subset_complete']):
        raise ValueError('initial subset cursor must be empty')
    return {'kind': KIND, 'plan_sha256': digest(plan), 'sequence': sequence, 'parent_sha256': parent_sha256,
            'nonce': nonce, 'state': copy.deepcopy(state), 'remote_mutations': 0, 'deletion_authorized': False}


def verify_transition(plan, before, after):
    """Accept one legal intent, verified receipt, or completion transition.

    A pending receipt reflects only a caller assertion; independently verified
    object absence, a durable conditional write and producer isolation remain
    mandatory outside this purely local envelope comparator.
    """
    for envelope in (before, after):
        if not isinstance(envelope, dict) or set(envelope) != {
                'kind', 'plan_sha256', 'sequence', 'parent_sha256', 'nonce', 'state',
                'remote_mutations', 'deletion_authorized'}:
            raise ValueError('invalid subset cursor envelope')
        expected = make_envelope(plan, envelope['state'], sequence=envelope['sequence'],
                                 parent_sha256=envelope['parent_sha256'], nonce=envelope['nonce'])
        if envelope != expected:
            raise ValueError('subset cursor envelope identity differs')
    if (after['sequence'] != before['sequence'] + 1 or after['parent_sha256'] != digest(before)
            or after['nonce'] == before['nonce']):
        raise ValueError('subset journal chain or nonce is invalid')
    old, new = before['state'], after['state']
    if old['subset_complete']:
        raise ValueError('completed subset cursor cannot advance')
    if (old['pending'] is None and new['pending'] is not None
            and new['receipts'] == old['receipts'] and new['subset_complete'] is False):
        transition = 'intent'
    elif (old['pending'] is not None and new['pending'] is None and new['subset_complete'] is False
          and len(new['receipts']) == len(old['receipts']) + 1
          and new['receipts'][:-1] == old['receipts']
          and new['receipts'][-1] == {**old['pending'], 'status': 'verified-absent'}):
        transition = 'receipt'
    elif (old['pending'] is None and new['pending'] is None and old['receipts'] == new['receipts']
          and new['subset_complete'] is True):
        transition = 'subset-complete'
    else:
        raise ValueError('subset cursor transition is not an exact single step')
    return {'transition': transition, 'sequence': after['sequence'], 'envelope_sha256': digest(after),
            'remote_mutations': 0, 'deletion_authorized': False}
