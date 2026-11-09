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
    from .search_space_transition import search_parent_bytes, _verify_candidate_sources
except ImportError:
    import search_adopt as adoption
    import search_consumer_receipts as receipts
    import search_default_receipts as defaults
    import search_pages_deploy as deploy
    import search_space_deploy as space_deploy
    import search_transition as tx
    from reader_bucket_store import HubBucketStore
    from search_daily import bounded_get
    from search_space_transition import search_parent_bytes, _verify_candidate_sources


REV = re.compile(r'[0-9a-f]{40}\Z')


def _pending(store, operation: str) -> tuple[tx.Current, dict]:
    current = tx.load(store)
    pending = current.record['pending'] if current is not None else None
    if (current is None or not isinstance(pending, dict) or pending.get('site') != 'pages'
            or pending.get('operation') not in {'apply', 'rollback'} or pending.get('operation_id') != operation):
        raise ValueError('no exact pending Pages operation')
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
    configured, updated = space_deploy.variable(api)
    if configured != current.record['candidate']:
        raise ValueError('Space configured generation is not the candidate')
    state = api.get_space_runtime(space_deploy.SPACE)
    if state.stage != 'RUNNING' or not isinstance(state.raw, dict) or not REV.fullmatch(str(state.raw.get('sha', ''))):
        raise ValueError('Space runtime is not running a known revision')
    space_deploy.runtime(api, expected_sha=state.raw['sha'])
    receipts.observe('space', current.record['candidate'], manifest, get=get)
    if space_deploy.variable(api) != (configured, updated):
        raise ValueError('Space configuration changed during candidate observation')
    space_deploy.runtime(api, expected_sha=state.raw['sha'])


def prepare(store, *, session, api, hub, get, expected_revision: str, operation_id: str, created_at: str,
            rollback: bool = False) -> tx.Current:
    current = tx.load(store)
    allowed = {'applying', 'verified', 'rolling-back'} if rollback else {'applying'}
    if current is None or current.record['state'] not in allowed or current.record['pending'] is not None:
        raise ValueError('search transaction is not ready for Pages')
    tx.audit_history(store, current)
    if not rollback and current.record['observed']['space']['generation'] != current.record['candidate']:
        raise ValueError('Space has not been durably observed on the candidate')
    if _main(session) != expected_revision:
        raise ValueError('Pages main differs from requested revision')
    deploy.assert_idle(session)
    if not rollback:
        _space_loaded(current, api=api, hub=hub, get=get)
    parent = current.record['parent_pointer']['desired']
    defaults.observe_pages(parent, session=session)
    observed = current.record['candidate'] if rollback else parent
    manifest = adoption.validate_generation(hub, observed)
    receipts.observe('pages', observed, manifest, get=get)
    if rollback:
        adoption.validate_generation(hub, parent)
    if _main(session) != expected_revision:
        raise ValueError('Pages main changed during intent preparation')
    defaults.observe_pages(parent, session=session)
    deploy.assert_idle(session)
    if not rollback:
        _space_loaded(current, api=api, hub=hub, get=get)
    return tx.start_site(store, current, site='pages', operation_id=operation_id,
                         expected_revision=expected_revision, created_at=created_at, rollback=rollback)


def apply_once(store, *, session, api, hub, get, operation_id: str) -> dict:
    current, pending = _pending(store, operation_id)
    if deploy.inspect_runs(session, operation=operation_id, not_before=pending['created_at'],
                           expected_sha=pending['expected_revision']) is not None:
        raise ValueError('Pages operation already has a run; reconcile instead of redispatch')
    if pending['operation'] == 'apply':
        _space_loaded(current, api=api, hub=hub, get=get)
        # Do not dispatch a newly stale generation after the Space switch.
        manifest = adoption.validate_generation(hub, current.record['candidate'])
        _verify_candidate_sources(manifest, current.record['source_revisions_sha256'])
        if store.read_pointer() != search_parent_bytes(current):
            raise ValueError('search desired pointer changed during Pages preflight')
    target = current.record['parent_pointer']['desired'] if pending['operation'] == 'rollback' else current.record['candidate']
    if pending['operation'] == 'rollback':
        adoption.validate_generation(hub, target)
    defaults.observe_pages(current.record['parent_pointer']['desired'], session=session)
    # A run not yet visible does not prove a prior dispatch was never sent.
    # Persist a unique write claim before issuing the one allowed request.
    current = tx.claim_write(store, current, site='pages', operation=pending['operation'], operation_id=operation_id)
    tx.check_write_claim(store, current)
    deploy.dispatch(session, generation=target, operation=operation_id, expected_sha=pending['expected_revision'])
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
    if pending['operation'] == 'apply':
        _space_loaded(current, api=api, hub=hub, get=get)
    target = current.record['parent_pointer']['desired'] if pending['operation'] == 'rollback' else current.record['candidate']
    defaults.observe_pages(current.record['parent_pointer']['desired'], session=session)
    manifest = adoption.validate_generation(hub, target)
    actual = receipts.observe('pages', target, manifest, get=get)
    if _main(session) != pending['expected_revision'] or store.read_pointer() != search_parent_bytes(current):
        raise ValueError('Pages or desired pointer changed during acceptance')
    defaults.observe_pages(current.record['parent_pointer']['desired'], session=session)
    deploy.assert_idle(session)
    if pending['operation'] == 'apply':
        _space_loaded(current, api=api, hub=hub, get=get)
    result = tx.observe_site(store, current, site='pages', generation=target, receipt=actual)
    status = 'parent-loaded' if pending['operation'] == 'rollback' else 'candidate-loaded'
    return {'kind': 'search-pages-observation', 'operation': operation_id, 'status': status,
            'run_id': run['id'], 'generation': target, 'sequence': result.record['sequence'],
            'journal_advanced': True, 'external_writers_verified': False}


def recover_failed_apply(store, *, session, hub, get, operation_id: str) -> dict:
    """Record parent after a reviewed workflow failed before deployment.

    Operators must coordinate both writers and anyone able to re-run the Pages
    workflow. A terminal response and two queue observations are not a fence;
    a failed deploy, missing run or unreviewed topology remains unresolved.
    """
    current, pending = _pending(store, operation_id)
    if pending['operation'] != 'apply':
        raise ValueError('only a failed apply can be accepted on the parent')
    run = deploy.inspect_runs(session, operation=operation_id, not_before=pending['created_at'],
                              expected_sha=pending['expected_revision'])
    if (run is None or run.get('status') != 'completed'
            or run.get('conclusion') not in {'failure', 'cancelled', 'timed_out'}):
        raise ValueError('no exact terminal failed Pages run; keep intent pending')
    deploy.failed_before_deploy(session, run, expected_sha=pending['expected_revision'])
    if _main(session) != pending['expected_revision']:
        raise ValueError('Pages main changed before failed-run recovery')
    deploy.assert_idle(session)
    parent = current.record['parent_pointer']['desired']
    defaults.observe_pages(parent, session=session)
    manifest = adoption.validate_generation(hub, parent)
    actual = receipts.observe('pages', parent, manifest, get=get)
    if _main(session) != pending['expected_revision'] or store.read_pointer() != search_parent_bytes(current):
        raise ValueError('Pages or desired pointer changed during failed-run recovery')
    defaults.observe_pages(parent, session=session)
    deploy.assert_idle(session)
    later = deploy.inspect_runs(session, operation=operation_id, not_before=pending['created_at'],
                               expected_sha=pending['expected_revision'])
    if later != run:
        raise ValueError('Pages run changed during failed-run recovery')
    deploy.failed_before_deploy(session, later, expected_sha=pending['expected_revision'])
    result = tx.observe_failed_apply(store, current, site='pages', receipt=actual)
    return {'kind': 'search-pages-failed-apply-observation', 'operation': operation_id,
            'run_id': run['id'], 'status': 'parent-loaded', 'generation': parent,
            'sequence': result.record['sequence'], 'journal_advanced': True, 'external_writers_verified': False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', required=True, choices=('prepare', 'rollback-prepare', 'apply', 'reconcile',
                                                          'recover-failed-apply'))
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
    if args.mode in {'prepare', 'rollback-prepare'}:
        if not REV.fullmatch(args.expected_revision) or args.operation:
            raise ValueError('prepare requires one exact Pages revision and no caller operation ID')
        op = secrets.token_hex(16); now = datetime.now(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')
        print(json.dumps({'kind': 'search-pages-intent-start', 'operation': op, 'revision': args.expected_revision},
                         sort_keys=True), flush=True)
        current = prepare(store, session=session, api=api, hub=hub, get=bounded_get,
                          expected_revision=args.expected_revision, operation_id=op, created_at=now,
                          rollback=args.mode == 'rollback-prepare')
        result = {'kind': 'search-pages-intent', 'operation': op, 'sequence': current.record['sequence'],
                  'candidate': current.record['candidate'], 'site_changed': False, 'pointer_written': False}
    elif args.mode == 'apply':
        result = apply_once(store, session=session, api=api, hub=hub, get=bounded_get, operation_id=args.operation)
    elif args.mode == 'recover-failed-apply':
        result = recover_failed_apply(store, session=session, hub=hub, get=bounded_get, operation_id=args.operation)
    else:
        result = reconcile(store, session=session, api=api, hub=hub, get=bounded_get, operation_id=args.operation)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
