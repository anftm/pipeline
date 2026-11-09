"""Prepare and reconcile a pre-pointer CCRD Space rollback.

Restores only the six paths from an exact durable activation intent. Candidate
or parent is determined by reading the current revision, files, runtime and
pointer; no retry is implied by absence. CAS and parent_commit do not fence an
already in-flight or unknown external writer. Post-pointer rollback is refused.
"""
from __future__ import annotations

import json

try:
    from . import ccrd_activation_inspection as activation
    from . import ccrd_activation_recovery as recovery
    from . import ccrd_consumer_inspection as consumer
    from . import ccrd_lifecycle as lifecycle
    from . import ccrd_publication_coordinator as coordinator
    from . import ccrd_space_activation as space
    from . import ccrd_transaction as tx
except ImportError:
    import ccrd_activation_inspection as activation
    import ccrd_activation_recovery as recovery
    import ccrd_consumer_inspection as consumer
    import ccrd_lifecycle as lifecycle
    import ccrd_publication_coordinator as coordinator
    import ccrd_space_activation as space
    import ccrd_transaction as tx


ROLLBACK_ALLOWED = {'space-intent', 'space-claimed', 'space-verified', 'pointer-intent', 'pointer-claimed'}


def _pointer(pointer_store, parent: bytes) -> str:
    raw, etag = pointer_store.read_for_update()
    if raw != parent:
        raise ValueError('CCRD rollback requires the exact pre-publication parent pointer')
    return tx.strong_etag(etag)


def _read_parent(plan: dict, *, get) -> dict[str, bytes]:
    expected = recovery._parent_map(plan)
    files = {}
    for path, item in expected.items():
        limit = item.get('bytes')
        if type(limit) is not int or not 0 < limit <= space.MAX_MEMBER:
            raise ValueError('CCRD rollback parent member size is invalid')
        raw = get(space._url(plan['parent_revision'], path), limit)
        recovery._identity(raw, item, path)
        files[path] = raw
    return files


def start(store, pointer_store, *, api, get=space._get, child_of=recovery.direct_child) -> tx.Current:
    """Record rollback intent only while pointer is still the exact parent."""
    current = coordinator._current(store, *sorted(ROLLBACK_ALLOWED))
    intent = coordinator._intent(store, current)
    parent = bytes.fromhex(intent['parent_pointer_hex']); plan = intent['activation_plan']
    etag = _pointer(pointer_store, parent)
    observed = recovery.observe(plan, api=api, get=get, child_of=child_of)
    if observed['status'] not in {'parent', 'candidate'}:
        raise ValueError('CCRD rollback Space state is unknown')
    if observed['status'] == 'candidate' and current.record['space_revision'] != observed['revision']:
        raise ValueError('CCRD candidate must be durably observed before rollback intent')
    _read_parent(plan, get=get)
    space._ocr_disabled(api, observed['revision'], get=get)
    space._revision(api, observed['revision'])
    if _pointer(pointer_store, parent) != etag:
        raise ValueError('CCRD parent pointer changed during rollback inspection')
    result = tx.advance(store, current, 'rollback-intent')
    if _pointer(pointer_store, parent) != etag:
        raise ValueError('CCRD parent pointer changed after rollback intent; halt and inspect')
    return result


def apply_once(store, pointer_store, *, api, get=space._get, child_of=recovery.direct_child) -> dict:
    """Restore the exact six parent paths with one parent-commit-guarded write."""
    current = coordinator._current(store, 'rollback-intent')
    intent = coordinator._intent(store, current)
    parent = bytes.fromhex(intent['parent_pointer_hex']); plan = intent['activation_plan']
    etag = _pointer(pointer_store, parent)
    observed = recovery.observe(plan, api=api, get=get, child_of=child_of)
    if observed['status'] != 'candidate':
        raise ValueError('CCRD candidate Space is not active; observe parent instead')
    if current.record['space_revision'] != observed['revision']:
        raise ValueError('CCRD candidate differs from the durably verified Space revision')
    files = _read_parent(plan, get=get)
    space._ocr_disabled(api, observed['revision'], get=get)
    space._revision(api, observed['revision'])
    if _pointer(pointer_store, parent) != etag:
        raise ValueError('CCRD parent pointer changed before rollback claim')
    claimed = tx.claim_write(store, current, operation='rollback')
    from huggingface_hub import CommitOperationAdd
    operations = [CommitOperationAdd(path_in_repo=path, path_or_fileobj=raw) for path, raw in sorted(files.items())]
    space._revision(api, observed['revision'])
    if _pointer(pointer_store, parent) != etag:
        raise ValueError('CCRD parent pointer changed after rollback claim')
    tx.check_write_claim(store, claimed, operation='rollback')
    commit = api.create_commit(repo_id=space.SPACE, repo_type='space', parent_commit=observed['revision'],
                               commit_message='Restore verified CCRD parent activation', operations=operations)
    revision = getattr(commit, 'oid', None)
    if not isinstance(revision, str) or not space.SHA.fullmatch(revision):
        raise RuntimeError('CCRD rollback commit response is uncertain; inspect before recovery')
    if api.space_info(space.SPACE, timeout=20).sha != revision:
        raise RuntimeError('CCRD rollback commit readback differs; inspect before recovery')
    return {'kind': 'ccrd-prepointer-space-rollback-request', 'revision': revision,
            'files_written': sorted(files), 'pointer_written': False, 'consumer_verified': False,
            'external_writers_verified': False}


def observe(store, pointer_store, *, api, get=space._get, child_of=recovery.direct_child,
            observe_activation=activation.observe, observe_consumer=consumer.observe) -> dict:
    """Verify actual restored paths, Space activation, consumer and exact pointer."""
    current = coordinator._current(store, 'rollback-intent', 'rollback-claimed')
    intent = coordinator._intent(store, current)
    parent_raw = bytes.fromhex(intent['parent_pointer_hex']); parent = json.loads(parent_raw)
    plan = intent['activation_plan']; etag = _pointer(pointer_store, parent_raw)
    observed_revision = getattr(api.space_info(space.SPACE, timeout=20), 'sha', None)
    if not isinstance(observed_revision, str) or not space.SHA.fullmatch(observed_revision):
        raise ValueError('CCRD rollback Space revision is unavailable')
    if observed_revision == plan['parent_revision']:
        status = 'parent'
    else:
        if current.record['state'] != 'rollback-claimed':
            raise ValueError('CCRD rollback child exists without a durable write claim')
        candidate_revision = current.record['space_revision']
        if candidate_revision is None or child_of(candidate_revision, observed_revision) is not True:
            raise ValueError('CCRD rollback revision is not the exact candidate direct child')
        status = 'restored-child'
    for path, expected in recovery._parent_map(plan).items():
        raw = get(space._url(observed_revision, path), expected['bytes'])
        recovery._identity(raw, expected, path)
    space._ocr_disabled(api, observed_revision, get=get)
    space._revision(api, observed_revision)
    activated = observe_activation(api, generation=parent['generation'], manifest_sha256=current.record['parent_sha256'])
    if (not isinstance(activated, dict) or activated.get('kind') != 'ccrd-space-activation-observation'
            or activated.get('revision') != observed_revision or activated.get('generation') != parent['generation']
            or activated.get('manifest_sha256') != current.record['parent_sha256']
            or activated.get('runtime_observed_twice') is not True):
        raise ValueError('CCRD rollback activation differs from exact parent')
    expected_text = parent['source_overrides'][str(space.source.CORPUS_PATH)]
    consumed = observe_consumer(parent['generation'], {key: expected_text[key] for key in ('bytes', 'sha256')})
    if (not isinstance(consumed, dict) or consumed.get('generation') != parent['generation']
            or consumed.get('cw0') != {key: expected_text[key] for key in ('bytes', 'sha256')}
            or consumed.get('cw0_bytes_verified') is not True or consumed.get('status_observed_twice') is not True
            or not isinstance(consumed.get('loaded_sources'), list)
            or sorted(consumed['loaded_sources']) != ['CCRD', 'CW', 'PLA', 'RMRB']):
        raise ValueError('CCRD rollback consumer did not verify parent and four sources')
    if _pointer(pointer_store, parent_raw) != etag:
        raise ValueError('CCRD parent pointer changed during rollback acceptance')
    activated_after = observe_activation(api, generation=parent['generation'], manifest_sha256=current.record['parent_sha256'])
    if activated_after != activated:
        raise ValueError('CCRD parent activation changed during rollback acceptance')
    space._ocr_disabled(api, observed_revision, get=get)
    space._revision(api, observed_revision)
    proof = lifecycle.canonical({'version': 1, 'kind': 'ccrd-rollback-evidence', 'revision': observed_revision,
                                 'parent_sha256': current.record['parent_sha256'],
                                 'candidate_generation': current.record['candidate_generation'],
                                 'space_plan_sha256': current.record['activation_plan_sha256'],
                                 'consumer_parent_verified': True, 'activation_parent_verified': True,
                                 'pointer_parent_verified': True, 'activation': activated, 'consumer': consumed,
                                 'restored_paths': sorted(recovery._parent_map(plan)), 'status': status})
    result = tx.advance(store, current, 'rolled-back', space_revision=observed_revision, evidence=proof)
    if _pointer(pointer_store, parent_raw) != etag:
        raise ValueError('CCRD parent pointer changed after rollback acceptance; halt and inspect')
    return {'kind': 'ccrd-prepointer-rollback-observation', 'status': status, 'revision': observed_revision,
            'journal_sequence': result.record['sequence'], 'pointer_written': False,
            'consumer_verified': True, 'external_writers_verified': False}
