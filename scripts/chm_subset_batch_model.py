"""Pure in-memory state transitions for exact CHM subset batches.

No bucket access, storage writes, delete callback, workflow, or CLI exists.
The model checks supplied observations only and returns hypothetical envelopes;
it never authorizes a deletion or marks the original transaction complete.
"""
import copy

try:
    from . import chm_subset_journal as journal, chm_subset_recovery_analysis as recovery
except ImportError:
    import chm_subset_journal as journal
    import chm_subset_recovery_analysis as recovery


def _check(rows, old_batches, selected, withheld, plan, head, observation):
    if (not isinstance(plan, dict) or plan.get('selected') != selected or plan.get('withheld') != withheld
            or plan.get('deletion_authorized') is not False or not isinstance(head, dict)):
        raise ValueError('subset batch plan or envelope differs')
    expected = journal.make_envelope(plan, head['state'], sequence=head['sequence'],
                                     parent_sha256=head['parent_sha256'], nonce=head['nonce'])
    if head != expected:
        raise ValueError('subset envelope is not bound to the exact plan')
    result = recovery.analyze(rows, old_batches, selected, withheld, head['state'], observation)
    return copy.deepcopy(head['state']), result


def plan_intent(rows, old_batches, selected, withheld, plan, head, observation, *, batch_size, nonce):
    """Plan one bounded hypothetical intent from an empty pending cursor."""
    state, result = _check(rows, old_batches, selected, withheld, plan, head, observation)
    if type(batch_size) is not int or not 1 <= batch_size <= 100:
        raise ValueError('invalid hypothetical batch size')
    if result['status'] != 'no-pending-intent' or state['subset_complete']:
        raise ValueError('pending or complete cursor cannot issue an intent')
    verified = sum(len(batch['paths']) for batch in state['receipts'])
    paths = [row['path'] for row in selected[verified:verified + batch_size]]
    if not paths:
        raise ValueError('no remaining subset batch')
    pending = {'bucket': journal.subset.ASSETS, 'status': 'submitted', 'paths': paths}
    after = journal.make_envelope(plan, {**state, 'pending': pending}, sequence=head['sequence'] + 1,
                                  parent_sha256=journal.digest(head), nonce=nonce)
    journal.verify_transition(plan, head, after)
    return {'kind': 'hypothetical-chm-subset-intent', 'envelope': after,
            'objects': len(paths), 'remote_mutations': 0, 'deletion_authorized': False}


def plan_receipt(rows, old_batches, selected, withheld, plan, head, observation, *, nonce):
    """Describe a possible receipt only after all pending paths are observed absent.

    This does not authenticate the observation, prove who deleted anything,
    or establish graph/consumer/lock barriers. Never publish this result as a
    production receipt without independently implementing those conditions.
    """
    state, result = _check(rows, old_batches, selected, withheld, plan, head, observation)
    if result['status'] != 'ready-for-independent-verification' or state['pending'] is None:
        raise ValueError('pending batch is not entirely observed absent')
    receipt = {**state['pending'], 'status': 'verified-absent'}
    after_state = {**state, 'pending': None, 'receipts': [*state['receipts'], receipt]}
    after = journal.make_envelope(plan, after_state, sequence=head['sequence'] + 1,
                                  parent_sha256=journal.digest(head), nonce=nonce)
    journal.verify_transition(plan, head, after)
    return {'kind': 'hypothetical-chm-subset-receipt', 'envelope': after,
            'objects': len(receipt['paths']), 'remote_mutations': 0, 'deletion_authorized': False}


def plan_completion(rows, old_batches, selected, withheld, plan, head, observation, *, nonce):
    """Describe only subset completion; original completion remains false."""
    state, result = _check(rows, old_batches, selected, withheld, plan, head, observation)
    verified = sum(len(batch['paths']) for batch in state['receipts'])
    if result['status'] != 'no-pending-intent' or verified != len(selected):
        raise ValueError('subset completion requires all selected receipts and no pending batch')
    after = journal.make_envelope(plan, {**state, 'subset_complete': True}, sequence=head['sequence'] + 1,
                                  parent_sha256=journal.digest(head), nonce=nonce)
    journal.verify_transition(plan, head, after)
    return {'kind': 'hypothetical-chm-subset-completion', 'envelope': after,
            'original_complete': False, 'producer_restore_authorized': False,
            'remote_mutations': 0, 'deletion_authorized': False}
