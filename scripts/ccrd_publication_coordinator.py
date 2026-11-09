"""Advance one CCRD publication intent by one explicitly verified step.

No automatic replay: a process that may have attempted a remote mutation must
call the matching read-only reconciliation before another write. External
writers still require coordination; the journal head CAS is not a global lock.
"""
from __future__ import annotations

import hashlib
import json

try:
    from . import ccrd_activation_inspection as activation
    from . import ccrd_activation_recovery as recovery
    from . import ccrd_consumer_inspection as consumer
    from . import ccrd_lifecycle as lifecycle
    from . import ccrd_pointer_transition as pointer
    from . import ccrd_space_activation as space
    from . import ccrd_staging_plan as planning
    from . import ccrd_transaction as tx
except ImportError:
    import ccrd_activation_inspection as activation
    import ccrd_activation_recovery as recovery
    import ccrd_consumer_inspection as consumer
    import ccrd_lifecycle as lifecycle
    import ccrd_pointer_transition as pointer
    import ccrd_space_activation as space
    import ccrd_staging_plan as planning
    import ccrd_transaction as tx


def _current(store, *expected_states: str) -> tx.Current:
    current = tx.load(store)
    if current is None or current.record['state'] not in expected_states:
        raise ValueError('CCRD transaction is not in the expected state: ' + ', '.join(expected_states))
    tx.audit(store, current)
    return current


def _intent(store, current: tx.Current) -> dict:
    raw = store.read_intent(tx._intent_key(current.record['transaction'], current.record['durable_intent_sha256']))
    if raw is None:
        raise ValueError('CCRD durable intent is missing')
    return tx._intent(raw, current.record)


def prepare(store, *, parent_pointer: bytes, candidate_plan: dict, description: dict, activation_plan: dict,
            pointer_store, transaction: str | None = None) -> tx.Current:
    parent_sha = hashlib.sha256(parent_pointer).hexdigest()
    if planning.make(parent_pointer, description) != candidate_plan:
        raise ValueError('CCRD transaction candidate differs from exact parent plan')
    candidate = lifecycle.canonical(candidate_plan['manifest'])
    intent = lifecycle.canonical({'version': 1, 'kind': 'ccrd-cw0-durable-intent',
                                  'parent_pointer_hex': parent_pointer.hex(), 'candidate_manifest_hex': candidate.hex(),
                                  'activation_plan': activation_plan})
    if (description.get('candidate_identity') != activation_plan.get('candidate_description_sha256')
            or candidate_plan.get('generation') != activation_plan.get('candidate_generation')
            or candidate_plan.get('manifest_sha256') != activation_plan.get('candidate_manifest_sha256')
            or activation_plan.get('parent_pointer_sha256') != parent_sha):
        raise ValueError('CCRD durable transaction does not bind the approved candidate')
    current = tx.load(store)
    if current is None:
        actual, etag = pointer_store.read_for_update()
        if actual != parent_pointer:
            raise ValueError('CCRD live pointer differs from first transaction parent')
        tx.strong_etag(etag)
        result = tx.begin(store, parent_sha256=parent_sha, candidate_generation=candidate_plan['generation'],
                          candidate_manifest_sha256=candidate_plan['manifest_sha256'],
                          activation_plan_sha256=activation_plan['plan_sha256'],
                          parent_space_revision=activation_plan['parent_revision'], durable_intent=intent, transaction=transaction)
        later, later_etag = pointer_store.read_for_update()
        if later != actual or tx.strong_etag(later_etag) != etag:
            raise ValueError('CCRD pointer changed during first intent; halt and inspect')
        return result
    return tx.begin_next(store, current, pointer_store=pointer_store, parent_sha256=parent_sha,
                         candidate_generation=candidate_plan['generation'],
                         candidate_manifest_sha256=candidate_plan['manifest_sha256'],
                         activation_plan_sha256=activation_plan['plan_sha256'],
                         parent_space_revision=activation_plan['parent_revision'], durable_intent=intent, transaction=transaction)


def start_space(store) -> tx.Current:
    return tx.advance(store, _current(store, 'prepared'), 'space-intent')


def apply_space_once(store, *, candidate_plan: dict, description: dict, api, staging_store, capture, observe,
                     candidate_root, new_text: bytes, current_manifest: bytes, candidate_manifest: bytes,
                     get=space._get) -> dict:
    """Claim once, then request one parent-guarded Space commit; never replay."""
    current = _current(store, 'space-intent')
    intent = _intent(store, current)
    parent_raw = bytes.fromhex(intent['parent_pointer_hex'])
    target_raw = bytes.fromhex(intent['candidate_manifest_hex'])
    plan = intent['activation_plan']
    if (planning.make(parent_raw, description) != candidate_plan
            or lifecycle.canonical(candidate_plan['manifest']) != target_raw
            or current_manifest != parent_raw or candidate_manifest != target_raw
            or description.get('candidate_identity') != plan.get('candidate_description_sha256')):
        raise ValueError('CCRD Space apply does not match the durable transaction intent')
    claimed = tx.claim_write(store, current, operation='space')
    return space.apply(plan, parent_raw, candidate_plan, description, api=api, store=staging_store,
                       capture=capture, observe=observe, candidate_root=candidate_root, new_text=new_text,
                       current_manifest=current_manifest, candidate_manifest=candidate_manifest,
                       confirm_plan_sha256=current.record['activation_plan_sha256'], get=get,
                       before_commit=lambda: tx.check_write_claim(store, claimed, operation='space'))


def observe_space(store, *, api, get=space._get, child_of=recovery.direct_child,
                  observe_activation=activation.observe, observe_consumer=consumer.observe) -> dict:
    """Reconcile a possibly-sent Space commit; never performs a write."""
    current = _current(store, 'space-intent', 'space-claimed'); intent = _intent(store, current)
    plan = intent['activation_plan']
    observed = recovery.observe(plan, api=api, get=get, child_of=child_of)
    result = {'status': observed['status'], 'observation': observed, 'journal_advanced': False}
    if observed['status'] != 'candidate':
        return result
    activation_result = observe_activation(api, generation=current.record['candidate_generation'],
                                           manifest_sha256=current.record['candidate_manifest_sha256'])
    if (not isinstance(activation_result, dict) or activation_result.get('revision') != observed['revision']
            or activation_result.get('kind') != 'ccrd-space-activation-observation'
            or activation_result.get('generation') != current.record['candidate_generation']
            or activation_result.get('manifest_sha256') != current.record['candidate_manifest_sha256']
            or activation_result.get('runtime_observed_twice') is not True):
        raise ValueError('CCRD activation and recovery identities differ')
    manifest = json.loads(bytes.fromhex(intent['candidate_manifest_hex']))
    expected = manifest['source_overrides'][str(space.source.CORPUS_PATH)]
    text = {key: expected[key] for key in ('sha256', 'bytes')}
    consumed = observe_consumer(current.record['candidate_generation'], text)
    if (not isinstance(consumed, dict) or consumed.get('generation') != current.record['candidate_generation']
            or consumed.get('cw0') != text or consumed.get('cw0_bytes_verified') is not True
            or consumed.get('status_observed_twice') is not True
            or not isinstance(consumed.get('loaded_sources'), list)
            or sorted(consumed['loaded_sources']) != ['CCRD', 'CW', 'PLA', 'RMRB']):
        raise ValueError('CCRD consumer did not load the intended text and four sources')
    evidence = lifecycle.canonical({'version': 1, 'kind': 'ccrd-activation-evidence',
                                    'revision': observed['revision'], 'generation': current.record['candidate_generation'],
                                    'manifest_sha256': current.record['candidate_manifest_sha256'],
                                    'space_plan_sha256': current.record['activation_plan_sha256'],
                                    'consumer_verified': True, 'activation_verified': True,
                                    'recovery': observed, 'activation': activation_result, 'consumer': consumed})
    advanced = tx.advance(store, current, 'space-verified', space_revision=observed['revision'], evidence=evidence)
    return {**result, 'journal_advanced': True, 'journal_sequence': advanced.record['sequence']}


def start_pointer(store) -> tx.Current:
    return tx.advance(store, _current(store, 'space-verified'), 'pointer-intent')


def _pointer_evidence(current: tx.Current, intent: dict, *, api, capture, observe_activation, observe_consumer) -> dict:
    manifest = json.loads(bytes.fromhex(intent['candidate_manifest_hex']))
    source = capture()
    # The candidate staging plan is checked separately by the caller.
    activated = observe_activation(api, generation=current.record['candidate_generation'],
                                   manifest_sha256=current.record['candidate_manifest_sha256'])
    if (not isinstance(activated, dict) or activated.get('revision') != current.record['space_revision']
            or activated.get('kind') != 'ccrd-space-activation-observation'
            or activated.get('runtime_observed_twice') is not True):
        raise ValueError('CCRD activation revision changed before pointer acceptance')
    override = manifest['source_overrides'][str(space.source.CORPUS_PATH)]
    consumed = observe_consumer(current.record['candidate_generation'],
                                {key: override[key] for key in ('bytes', 'sha256')})
    source_after = capture()
    activated_after = observe_activation(api, generation=current.record['candidate_generation'],
                                         manifest_sha256=current.record['candidate_manifest_sha256'])
    if source_after != source or activated_after != activated:
        raise ValueError('CCRD source or activation changed during pointer acceptance')
    # pointer.apply validates full shapes and identities; this function performs
    # the live reads instead of accepting caller-provided evidence dictionaries.
    return {'generation': current.record['candidate_generation'], 'manifest_sha256': current.record['candidate_manifest_sha256'],
            'activation': activated, 'consumer': consumed, 'source': source}


def apply_pointer_once(store, pointer_store, *, candidate_plan: dict, description: dict, staging_store,
                       api, capture, observe_activation=activation.observe, observe_consumer=consumer.observe) -> dict:
    """Verify fresh live evidence, then claim and perform a single pointer CAS."""
    current = _current(store, 'pointer-intent'); intent = _intent(store, current)
    parent = bytes.fromhex(intent['parent_pointer_hex']); candidate = bytes.fromhex(intent['candidate_manifest_hex'])
    if (planning.make(parent, description) != candidate_plan or lifecycle.canonical(candidate_plan['manifest']) != candidate
            or description.get('candidate_identity') != intent['activation_plan'].get('candidate_description_sha256')):
        raise ValueError('CCRD pointer apply differs from durable transaction intent')
    _immutable_candidate(staging_store, current, candidate)
    evidence = _pointer_evidence(current, intent, api=api, capture=capture,
                                 observe_activation=observe_activation, observe_consumer=observe_consumer)
    raw, etag = pointer_store.read_for_update()
    if raw != parent:
        raise ValueError('CCRD pointer is not the exact parent before claim')
    pointer.validate_evidence(candidate_plan, immutable_manifest=candidate, evidence=evidence)
    claimed = tx.claim_write(store, current, operation='pointer')
    refreshed = _pointer_evidence(claimed, intent, api=api, capture=capture,
                                  observe_activation=observe_activation, observe_consumer=observe_consumer)
    pointer.validate_evidence(candidate_plan, immutable_manifest=candidate, evidence=refreshed)
    _immutable_candidate(staging_store, claimed, candidate)
    tx.check_write_claim(store, claimed, operation='pointer')
    return pointer.apply(candidate_plan, approved_description=description, expected_parent=parent,
                         expected_etag=etag, store=pointer_store, immutable_manifest=candidate, evidence=refreshed)


def _immutable_candidate(staging_store, current: tx.Current, expected: bytes) -> None:
    """Read candidate bytes from the confined generation adapter, never a caller receipt."""
    if getattr(staging_store, 'generation', None) != current.record['candidate_generation']:
        raise ValueError('CCRD immutable manifest reader points to a different generation')
    if staging_store.read_manifest() != expected:
        raise ValueError('CCRD immutable manifest is missing or differs from durable intent')


def finish(store, pointer_store, *, candidate_plan: dict, description: dict, staging_store,
           api, capture, observe_activation=activation.observe, observe_consumer=consumer.observe) -> tx.Current:
    """Finish only after fresh pointer, immutable manifest, Space and consumer reads.

    No remote mutation occurs here. Two pointer observations cannot fence a
    writer or prevent ABA; external publishers must still be coordinated.
    """
    current = _current(store, 'pointer-verified'); intent = _intent(store, current)
    parent = bytes.fromhex(intent['parent_pointer_hex']); candidate = bytes.fromhex(intent['candidate_manifest_hex'])
    if (planning.make(parent, description) != candidate_plan or lifecycle.canonical(candidate_plan['manifest']) != candidate
            or description.get('candidate_identity') != intent['activation_plan'].get('candidate_description_sha256')):
        raise ValueError('CCRD completion plan differs from durable intent')
    raw, etag = pointer_store.read_for_update()
    if raw != candidate:
        raise ValueError('CCRD current pointer is not the exact candidate at completion')
    tx.strong_etag(etag)
    _immutable_candidate(staging_store, current, candidate)
    evidence = _pointer_evidence(current, intent, api=api, capture=capture,
                                 observe_activation=observe_activation, observe_consumer=observe_consumer)
    pointer.validate_evidence(candidate_plan, immutable_manifest=candidate, evidence=evidence)
    _immutable_candidate(staging_store, current, candidate)
    later, later_etag = pointer_store.read_for_update()
    if later != raw or tx.strong_etag(later_etag) != etag:
        raise ValueError('CCRD pointer changed during completion')
    result = tx.advance(store, current, 'complete')
    after, after_etag = pointer_store.read_for_update()
    if after != raw or tx.strong_etag(after_etag) != etag:
        raise ValueError('CCRD pointer changed after completion; halt and inspect')
    return result


def observe_pointer(store, pointer_store, *, candidate_plan: dict, description: dict, staging_store,
                    api, capture, observe_activation=activation.observe, observe_consumer=consumer.observe) -> dict:
    """Reconcile exact pointer with fresh activation, source and consumer reads."""
    current = _current(store, 'pointer-intent', 'pointer-claimed'); intent = _intent(store, current)
    raw, etag = pointer_store.read_for_update()
    parent = bytes.fromhex(intent['parent_pointer_hex']); candidate = bytes.fromhex(intent['candidate_manifest_hex'])
    if raw == parent:
        return {'status': 'parent', 'etag': etag, 'journal_advanced': False}
    if raw != candidate:
        raise ValueError('CCRD pointer is neither exact parent nor intended candidate')
    if (planning.make(parent, description) != candidate_plan or lifecycle.canonical(candidate_plan['manifest']) != candidate
            or description.get('candidate_identity') != intent['activation_plan'].get('candidate_description_sha256')):
        raise ValueError('CCRD pointer observation differs from durable transaction intent')
    _immutable_candidate(staging_store, current, candidate)
    evidence = _pointer_evidence(current, intent, api=api, capture=capture,
                                 observe_activation=observe_activation, observe_consumer=observe_consumer)
    pointer.validate_evidence(candidate_plan, immutable_manifest=candidate, evidence=evidence)
    _immutable_candidate(staging_store, current, candidate)
    later, later_etag = pointer_store.read_for_update()
    if later != raw or tx.strong_etag(later_etag) != tx.strong_etag(etag):
        raise ValueError('CCRD pointer changed during acceptance')
    advanced = tx.advance(store, current, 'pointer-verified')
    return {'status': 'candidate', 'etag': etag, 'journal_advanced': True,
            'journal_sequence': advanced.record['sequence']}
