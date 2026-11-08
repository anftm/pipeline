"""Persist, apply once, and independently reconcile one Space search-data switch.

The operation ID is journaled before the Space variable changes.  A timeout or
unknown response is resolved only by observing exact revision, variable and
loaded generation; this module does not automatically replay a failed write.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
from datetime import UTC, datetime

try:
    from . import search_adopt as adoption
    from . import search_consumer_receipts as receipts
    from . import search_space_deploy as deploy
    from . import search_transition as tx
    from .reader_bucket_store import HubBucketStore
except ImportError:
    import search_adopt as adoption
    import search_consumer_receipts as receipts
    import search_space_deploy as deploy
    import search_transition as tx
    from reader_bucket_store import HubBucketStore


SHA = re.compile(r'[0-9a-f]{40}\Z')


def prepare(store, *, api, expected_revision: str, operation_id: str, created_at: str) -> tx.Current:
    current = tx.load(store)
    if current is None or current.record['state'] not in {'prepared', 'applying'} or current.record['pending'] is not None:
        raise ValueError('search transaction is not ready for a Space operation')
    tx.audit_history(store, current)
    parent = current.record['parent_pointer']['desired']
    if current.record['observed']['space']['generation'] != parent:
        raise ValueError('Space does not have the expected parent observation')
    deploy.runtime(api, expected_sha=expected_revision)
    configured, updated = deploy.variable(api)
    if configured != parent:
        raise ValueError('Space configured generation differs from expected parent')
    return tx.start_site(store, current, site='space', operation_id=operation_id, expected_revision=expected_revision,
                         created_at=created_at, config_before={'generation': configured, 'updated_at': updated})


def apply_once(store, *, api, operation_id: str) -> dict:
    current = tx.load(store)
    pending = current.record['pending'] if current is not None else None
    if (current is None or not isinstance(pending, dict) or pending.get('site') != 'space'
            or pending.get('operation') != 'apply' or pending.get('operation_id') != operation_id):
        raise ValueError('no exact pending Space apply operation')
    if store.read_pointer() != search_parent_bytes(current):
        raise ValueError('search desired pointer changed before Space update')
    before = pending['config_before']
    deploy.set_generation(api, expected=before['generation'], expected_updated_at=before['updated_at'],
                          target=current.record['candidate'], expected_sha=pending['expected_revision'])
    return {'kind': 'search-space-update-request', 'operation': operation_id, 'variable_written': True,
            'loaded_verified': False, 'journal_advanced': False, 'external_writers_verified': False}


def search_parent_bytes(current: tx.Current) -> bytes:
    try:
        from . import search_snapshot
    except ImportError:
        import search_snapshot
    return search_snapshot.canonical(current.record['parent_pointer'])


def reconcile(store, *, api, hub, get, operation_id: str) -> dict:
    current = tx.load(store)
    pending = current.record['pending'] if current is not None else None
    if (current is None or not isinstance(pending, dict) or pending.get('site') != 'space'
            or pending.get('operation') != 'apply' or pending.get('operation_id') != operation_id):
        raise ValueError('no exact pending Space apply operation')
    tx.audit_history(store, current)
    if store.read_pointer() != search_parent_bytes(current):
        raise ValueError('search desired pointer changed before Space reconciliation')
    configured, updated = deploy.variable(api)
    parent = pending['config_before']['generation']
    if configured == parent:
        return {'kind': 'search-space-observation', 'operation': operation_id, 'status': 'parent-configured',
                'journal_advanced': False, 'external_writers_verified': False}
    if configured != current.record['candidate']:
        raise ValueError('Space configuration is neither parent nor candidate')
    if deploy._timestamp(updated) < deploy._timestamp(pending['config_before']['updated_at']):
        raise ValueError('Space configuration timestamp moved backwards')
    state = api.get_space_runtime(deploy.SPACE)
    if state.stage == 'RUNNING_BUILDING':
        return {'kind': 'search-space-observation', 'operation': operation_id, 'status': 'building',
                'journal_advanced': False, 'external_writers_verified': False}
    if state.stage != 'RUNNING' or not isinstance(state.raw, dict) or state.raw.get('sha') != pending['expected_revision']:
        raise ValueError('Space runtime is unavailable or running an unexpected revision')
    deploy.runtime(api, expected_sha=pending['expected_revision'])
    manifest = adoption.validate_generation(hub, current.record['candidate'])
    actual = receipts.observe('space', current.record['candidate'], manifest, get=get)
    if store.read_pointer() != search_parent_bytes(current):
        raise ValueError('search desired pointer changed during Space reconciliation')
    configured_after, updated_after = deploy.variable(api)
    if (configured_after, updated_after) != (configured, updated):
        raise ValueError('Space configuration changed during reconciliation')
    deploy.runtime(api, expected_sha=pending['expected_revision'])
    result = tx.observe_site(store, current, site='space', generation=current.record['candidate'], receipt=actual)
    return {'kind': 'search-space-observation', 'operation': operation_id, 'status': 'candidate-loaded',
            'generation': result.record['candidate'], 'journal_advanced': True, 'sequence': result.record['sequence'],
            'external_writers_verified': False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', required=True, choices=('prepare', 'apply', 'reconcile'))
    parser.add_argument('--expected-revision', default='')
    parser.add_argument('--operation', default='')
    args = parser.parse_args(argv)
    from huggingface_hub import HfApi
    api = HfApi(token=os.environ.get('HF_TOKEN')); store = tx.S3TransitionStore()
    if args.mode == 'prepare':
        if not SHA.fullmatch(args.expected_revision) or args.operation:
            raise ValueError('prepare requires one exact Space revision and no caller operation ID')
        op = secrets.token_hex(16); now = datetime.now(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')
        print(json.dumps({'kind': 'search-space-intent-start', 'operation': op, 'revision': args.expected_revision},
                         sort_keys=True), flush=True)
        current = prepare(store, api=api, expected_revision=args.expected_revision, operation_id=op, created_at=now)
        result = {'kind': 'search-space-intent', 'operation': op, 'sequence': current.record['sequence'],
                  'candidate': current.record['candidate'], 'site_changed': False, 'pointer_written': False}
    elif args.mode == 'apply':
        result = apply_once(store, api=api, operation_id=args.operation)
    else:
        try:
            from .search_daily import bounded_get
        except ImportError:
            from search_daily import bounded_get
        result = reconcile(store, api=api, hub=HubBucketStore(), get=bounded_get, operation_id=args.operation)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
