"""Persist, dispatch once, and reconcile one Pages search-data deployment.

A workflow completion is not a consumer receipt. Exact main revision, unique
operation ID, run result, live deployed corpus and Space state are separate
observations. Unknown dispatch outcomes must be inspected, not replayed.
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
    from . import search_default_receipts as defaults
    from . import search_pages_deploy as deploy
    from . import search_space_deploy as space_deploy
    from . import search_transition as tx
    from .reader_bucket_store import HubBucketStore
    from .search_daily import bounded_get
    from .search_space_transition import search_parent_bytes
except ImportError:
    import search_adopt as adoption
    import search_consumer_receipts as receipts
    import search_default_receipts as defaults
    import search_pages_deploy as deploy
    import search_space_deploy as space_deploy
    import search_transition as tx
    from reader_bucket_store import HubBucketStore
    from search_daily import bounded_get
    from search_space_transition import search_parent_bytes


REV = re.compile(r'[0-9a-f]{40}\Z')


def _pending(store, operation: str) -> tuple[tx.Current, dict]:
    current = tx.load(store)
    pending = current.record['pending'] if current is not None else None
    if (current is None or not isinstance(pending, dict) or pending.get('site') != 'pages'
            or pending.get('operation') != 'apply' or pending.get('operation_id') != operation):
        raise ValueError('no exact pending Pages apply operation')
    tx.audit_history(store, current)
    if store.read_pointer() != search_parent_bytes(current):
        raise ValueError('search desired pointer changed during Pages operation')
    return current, pending


def _main(session) -> str:
    response = session.get(f'{deploy.BASE}/branches/main', timeout=20); response.raise_for_status()
    value = response.json(); revision = value.get('commit', {}).get('sha') if isinstance(value, dict) else None
    if not isinstance(revision, str) or not REV.fullmatch(revision):
        raise ValueError('Pages main revision is unavailable')
    return revision


def _space_loaded(current: tx.Current, *, api, hub, get) -> None:
    manifest = adoption.validate_generation(hub, current.record['candidate'])
    configured, _updated = space_deploy.variable(api)
    if configured != current.record['candidate']:
        raise ValueError('Space configured generation is not the candidate')
    state = api.get_space_runtime(space_deploy.SPACE)
    if state.stage != 'RUNNING' or not isinstance(state.raw, dict) or not REV.fullmatch(str(state.raw.get('sha', ''))):
        raise ValueError('Space runtime is not running a known revision')
    space_deploy.runtime(api, expected_sha=state.raw['sha'])
    receipts.observe('space', current.record['candidate'], manifest, get=get)


def prepare(store, *, session, api, hub, get, expected_revision: str, operation_id: str, created_at: str) -> tx.Current:
    current = tx.load(store)
    if current is None or current.record['state'] != 'applying' or current.record['pending'] is not None:
        raise ValueError('search transaction is not ready for Pages')
    if current.record['observed']['space']['generation'] != current.record['candidate']:
        raise ValueError('Space has not been durably observed on the candidate')
    if _main(session) != expected_revision:
        raise ValueError('Pages main differs from requested revision')
    deploy.assert_idle(session)
    _space_loaded(current, api=api, hub=hub, get=get)
    parent = current.record['parent_pointer']['desired']
    defaults.observe_pages(parent, session=session)
    manifest = adoption.validate_generation(hub, parent)
    receipts.observe('pages', parent, manifest, get=get)
    return tx.start_site(store, current, site='pages', operation_id=operation_id,
                         expected_revision=expected_revision, created_at=created_at)


def apply_once(store, *, session, api, hub, get, operation_id: str) -> dict:
    current, pending = _pending(store, operation_id)
    if deploy.inspect_runs(session, operation=operation_id, not_before=pending['created_at'],
                           expected_sha=pending['expected_revision']) is not None:
        raise ValueError('Pages operation already has a run; reconcile instead of redispatch')
    _space_loaded(current, api=api, hub=hub, get=get)
    deploy.dispatch(session, generation=current.record['candidate'], operation=operation_id,
                    expected_sha=pending['expected_revision'])
    return {'kind': 'search-pages-dispatch', 'operation': operation_id, 'dispatch_confirmed': True,
            'loaded_verified': False, 'journal_advanced': False, 'external_writers_verified': False}


def reconcile(store, *, session, api, hub, get, operation_id: str) -> dict:
    current, pending = _pending(store, operation_id)
    run = deploy.inspect_runs(session, operation=operation_id, not_before=pending['created_at'],
                              expected_sha=pending['expected_revision'])
    if run is None:
        return {'kind': 'search-pages-observation', 'operation': operation_id, 'status': 'run-not-visible',
                'journal_advanced': False, 'external_writers_verified': False}
    if run['status'] != 'completed':
        return {'kind': 'search-pages-observation', 'operation': operation_id, 'status': 'deploying',
                'run_id': run['id'], 'journal_advanced': False, 'external_writers_verified': False}
    if run['conclusion'] != 'success':
        raise ValueError('Pages deployment failed; inspect before recovery')
    if _main(session) != pending['expected_revision']:
        raise ValueError('Pages main changed before deployment acceptance')
    deploy.assert_idle(session)
    _space_loaded(current, api=api, hub=hub, get=get)
    defaults.observe_pages(current.record['parent_pointer']['desired'], session=session)
    manifest = adoption.validate_generation(hub, current.record['candidate'])
    actual = receipts.observe('pages', current.record['candidate'], manifest, get=get)
    if _main(session) != pending['expected_revision'] or store.read_pointer() != search_parent_bytes(current):
        raise ValueError('Pages or desired pointer changed during acceptance')
    deploy.assert_idle(session)
    result = tx.observe_site(store, current, site='pages', generation=current.record['candidate'], receipt=actual)
    return {'kind': 'search-pages-observation', 'operation': operation_id, 'status': 'candidate-loaded',
            'run_id': run['id'], 'generation': result.record['candidate'], 'sequence': result.record['sequence'],
            'journal_advanced': True, 'external_writers_verified': False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', required=True, choices=('prepare', 'apply', 'reconcile'))
    parser.add_argument('--expected-revision', default='')
    parser.add_argument('--operation', default='')
    args = parser.parse_args(argv)
    import requests
    from huggingface_hub import HfApi
    token = os.environ.get('PAGES_TOKEN', '')
    if not token: raise ValueError('existing Pages credential is required')
    session = requests.Session(); session.headers.update({'Authorization': 'Bearer ' + token,
                                                           'Accept': 'application/vnd.github+json',
                                                           'User-Agent': 'pipeline-search-pages-transition/1.0'})
    api = HfApi(token=os.environ.get('HF_TOKEN')); hub = HubBucketStore(); store = tx.S3TransitionStore()
    if args.mode == 'prepare':
        if not REV.fullmatch(args.expected_revision) or args.operation:
            raise ValueError('prepare requires one exact Pages revision and no caller operation ID')
        op = secrets.token_hex(16); now = datetime.now(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')
        print(json.dumps({'kind': 'search-pages-intent-start', 'operation': op, 'revision': args.expected_revision},
                         sort_keys=True), flush=True)
        current = prepare(store, session=session, api=api, hub=hub, get=bounded_get,
                          expected_revision=args.expected_revision, operation_id=op, created_at=now)
        result = {'kind': 'search-pages-intent', 'operation': op, 'sequence': current.record['sequence'],
                  'candidate': current.record['candidate'], 'site_changed': False, 'pointer_written': False}
    elif args.mode == 'apply':
        result = apply_once(store, session=session, api=api, hub=hub, get=bounded_get, operation_id=args.operation)
    else:
        result = reconcile(store, session=session, api=api, hub=hub, get=bounded_get, operation_id=args.operation)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
