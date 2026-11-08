"""Explicitly publish and finalize a two-site verified search transition.

Each invocation performs one journal/CAS/variable step. A lost write response
must be inspected before any retry. Source and site observations are rechecked;
coordination of independent writers remains a separate responsibility.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
from datetime import UTC, datetime

try:
    from . import fetch_and_parse as fetch
    from . import search_adopt as adoption
    from . import search_consumer_receipts as consumers
    from . import search_default_receipts as defaults
    from . import search_pages_deploy as pages
    from . import search_space_deploy as space
    from . import search_transition as tx
    from .reader_bucket_store import HubBucketStore
    from .search_daily import bounded_get
    from .search_publication_pointer import S3SearchPointerStore, make_pointer
except ImportError:
    import fetch_and_parse as fetch
    import search_adopt as adoption
    import search_consumer_receipts as consumers
    import search_default_receipts as defaults
    import search_pages_deploy as pages
    import search_space_deploy as space
    import search_transition as tx
    from reader_bucket_store import HubBucketStore
    from search_daily import bounded_get
    from search_publication_pointer import S3SearchPointerStore, make_pointer


VARIABLE = defaults.VARIABLE


def _current(store, state: str) -> tx.Current:
    current = tx.load(store)
    if current is None or current.record['state'] != state:
        raise ValueError('search transition is not in state ' + state)
    tx.audit_history(store, current)
    return current


def _actual(current: tx.Current, *, hub, api, session, get, revision) -> tuple[bytes, bytes]:
    candidate = current.record['candidate']; manifest = adoption.validate_generation(hub, candidate)
    revisions = manifest.get('source_revisions')
    if not isinstance(revisions, dict) or set(revisions) != set(fetch.REPOS):
        raise ValueError('search candidate source revisions are incomplete')
    token = os.environ.get('HF_TOKEN', '')
    if {repo: revision(repo, token) for repo in fetch.REPOS} != revisions:
        raise ValueError('search source revisions changed before promotion')
    configured, _ = space.variable(api)
    if configured != candidate:
        raise ValueError('Space default is not the candidate')
    runtime = api.get_space_runtime(space.SPACE)
    if runtime.stage != 'RUNNING' or not isinstance(runtime.raw, dict):
        raise ValueError('Space runtime is not running')
    space.runtime(api, expected_sha=runtime.raw.get('sha', ''))
    pages.assert_idle(session)
    first = consumers.observe('pages', candidate, manifest, get=get)
    second = consumers.observe('space', candidate, manifest, get=get)
    if {repo: revision(repo, token) for repo in fetch.REPOS} != revisions:
        raise ValueError('search source revisions changed during promotion')
    if space.variable(api)[0] != candidate:
        raise ValueError('Space configuration changed during promotion')
    pages.assert_idle(session)
    return first, second


def mark_publishing(store, *, hub, api, session, get, revision) -> tx.Current:
    current = _current(store, 'verified')
    first, second = _actual(current, hub=hub, api=api, session=session, get=get, revision=revision)
    return tx.mark_publishing(store, current, pages_receipt=first, space_receipt=second)


def publish(store, pointer_store, *, hub, api, session, get, revision) -> tx.Current:
    current = _current(store, 'publishing')
    first, second = _actual(current, hub=hub, api=api, session=session, get=get, revision=revision)
    return tx.publish_pointer(store, pointer_store, current, pages_receipt=first, space_receipt=second)


def reconcile_pointer(store, *, hub, api, session, get, revision) -> tx.Current:
    current = _current(store, 'publishing')
    first, second = _actual(current, hub=hub, api=api, session=session, get=get, revision=revision)
    intended = make_pointer(current.record['candidate'], current.record['parent_pointer']['desired'],
                            current.record['transition'])
    return tx.record_pointer(store, current, observed_pointer=intended, pages_receipt=first, space_receipt=second)


def observe_space_default(store, *, hub, api, session, get, revision, expected_revision, operation, created_at) -> tx.Current:
    current = _current(store, 'pointer-observed')
    _actual(current, hub=hub, api=api, session=session, get=get, revision=revision)
    space.runtime(api, expected_sha=expected_revision)
    pending = current.record['pending']
    if pending is None:
        current = tx.start_default(store, current, site='space', operation_id=operation,
                                   expected_revision=expected_revision, created_at=created_at)
        pending = current.record['pending']
    elif (pending.get('site') != 'space' or pending.get('operation') != 'set-default'
          or pending.get('expected_revision') != expected_revision):
        raise ValueError('no matching Space default observation intent')
    # The prior process may have stopped after persisting the intent. Re-read
    # the live default for both first acceptance and recovery; never mutate it.
    receipt = defaults.observe_space(current.record['candidate'], api=api)
    space.runtime(api, expected_sha=expected_revision)
    return tx.observe_default(store, current, site='space', receipt=receipt)


def start_pages_default(store, *, session, expected_revision: str, operation: str, created_at: str) -> tx.Current:
    current = _current(store, 'pointer-observed')
    response = session.get(f'{pages.BASE}/branches/main', timeout=20); response.raise_for_status()
    if response.json().get('commit', {}).get('sha') != expected_revision:
        raise ValueError('Pages main changed before default intent')
    pages.assert_idle(session)
    defaults.observe_pages(current.record['parent_pointer']['desired'], session=session)
    return tx.start_default(store, current, site='pages', operation_id=operation,
                            expected_revision=expected_revision, created_at=created_at)


def set_pages_default(store, *, session, api, hub, get, revision, operation: str) -> dict:
    current = _current(store, 'pointer-observed'); pending = current.record['pending']
    if (not isinstance(pending, dict) or pending.get('site') != 'pages'
            or pending.get('operation') != 'set-default' or pending.get('operation_id') != operation):
        raise ValueError('no exact Pages default update intent')
    response = session.get(f'{pages.BASE}/branches/main', timeout=20); response.raise_for_status()
    if response.json().get('commit', {}).get('sha') != pending['expected_revision']:
        raise ValueError('Pages main changed before default update')
    pages.assert_idle(session)
    _actual(current, hub=hub, api=api, session=session, get=get, revision=revision)
    defaults.observe_pages(current.record['parent_pointer']['desired'], session=session)
    url = f'{pages.BASE}/actions/variables/{VARIABLE}'
    # The claim survives process death and prevents another invocation from
    # replaying an uncertain PATCH. A claim is not an external writer fence.
    current = tx.claim_write(store, current, site='pages', operation='set-default', operation_id=operation)
    tx.check_write_claim(store, current)
    response = session.patch(url, json={'name': VARIABLE, 'value': current.record['candidate']}, timeout=25)
    if response.status_code != 204:
        raise RuntimeError('Pages default update outcome is unconfirmed; inspect before recovery')
    defaults.observe_pages(current.record['candidate'], session=session)
    return {'kind': 'search-pages-default-update', 'operation': operation, 'variable_written': True,
            'journal_advanced': False, 'external_writers_verified': False}


def reconcile_pages_default(store, *, session, api, hub, get, revision, operation: str) -> tx.Current:
    current = _current(store, 'pointer-observed'); pending = current.record['pending']
    if (not isinstance(pending, dict) or pending.get('site') != 'pages'
            or pending.get('operation') != 'set-default' or pending.get('operation_id') != operation):
        raise ValueError('no exact Pages default update intent')
    response = session.get(f'{pages.BASE}/branches/main', timeout=20); response.raise_for_status()
    if response.json().get('commit', {}).get('sha') != pending['expected_revision']:
        raise ValueError('Pages main changed before default reconciliation')
    _actual(current, hub=hub, api=api, session=session, get=get, revision=revision)
    receipt = defaults.observe_pages(current.record['candidate'], session=session)
    return tx.observe_default(store, current, site='pages', receipt=receipt)


def finish(store, *, session, api, hub, get, revision) -> tx.Current:
    current = _current(store, 'pointer-observed')
    first, second = _actual(current, hub=hub, api=api, session=session, get=get, revision=revision)
    return tx.finish(store, current, pages_receipt=first, space_receipt=second,
                     pages_default=defaults.observe_pages(current.record['candidate'], session=session),
                     space_default=defaults.observe_space(current.record['candidate'], api=api))


def finish_rollback(store, *, session, api, hub, get) -> tx.Current:
    """Accept rollback only after both actual sites and defaults are parent."""
    current = _current(store, 'rolling-back')
    if current.record['pending'] is not None:
        raise ValueError('rollback has a pending site operation')
    parent = current.record['parent_pointer']['desired']
    response = session.get(f'{pages.BASE}/branches/main', timeout=20); response.raise_for_status()
    main = response.json().get('commit', {}).get('sha')
    if not isinstance(main, str) or not pages.SHA.fullmatch(main):
        raise ValueError('Pages main revision is unavailable')
    manifest = adoption.validate_generation(hub, parent)
    pages.assert_idle(session)
    configured, updated = space.variable(api)
    if configured != parent:
        raise ValueError('Space configured generation is not the parent')
    runtime = api.get_space_runtime(space.SPACE)
    if runtime.stage != 'RUNNING' or not isinstance(runtime.raw, dict):
        raise ValueError('Space runtime is not running')
    sha = runtime.raw.get('sha', '')
    space.runtime(api, expected_sha=sha)
    page_default = defaults.observe_pages(parent, session=session)
    space_default = defaults.observe_space(parent, api=api)
    first = consumers.observe('pages', parent, manifest, get=get)
    second = consumers.observe('space', parent, manifest, get=get)
    if space.variable(api) != (configured, updated):
        raise ValueError('Space configuration changed during rollback acceptance')
    space.runtime(api, expected_sha=sha)
    defaults.observe_pages(parent, session=session)
    pages.assert_idle(session)
    response = session.get(f'{pages.BASE}/branches/main', timeout=20); response.raise_for_status()
    if response.json().get('commit', {}).get('sha') != main:
        raise ValueError('Pages main changed during rollback acceptance')
    return tx.finish_rollback(store, current, pages_receipt=first, space_receipt=second,
                              pages_default=page_default, space_default=space_default)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', required=True, choices=('mark', 'publish', 'reconcile-pointer', 'space-default',
                                                          'pages-default-prepare', 'pages-default-apply',
                                                          'pages-default-reconcile', 'finish', 'rollback-finish',
                                                          'abort-unpublished'))
    parser.add_argument('--expected-revision', default=''); parser.add_argument('--operation', default='')
    args = parser.parse_args(argv)
    import requests
    from huggingface_hub import HfApi
    token = os.environ.get('PAGES_TOKEN', '')
    if not token: raise ValueError('existing Pages credential is required')
    session = requests.Session(); session.headers.update({'Authorization': 'Bearer ' + token,
                                                           'Accept': 'application/vnd.github+json',
                                                           'User-Agent': 'pipeline-search-finalize/1.0'})
    api = HfApi(token=os.environ.get('HF_TOKEN')); hub = HubBucketStore(); store = tx.S3TransitionStore()
    common = {'hub': hub, 'api': api, 'session': session, 'get': bounded_get, 'revision': fetch.get_repo_sha}
    op = args.operation; now = datetime.now(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')
    if args.mode == 'mark': current = mark_publishing(store, **common)
    elif args.mode == 'publish': current = publish(store, S3SearchPointerStore(), **common)
    elif args.mode == 'reconcile-pointer': current = reconcile_pointer(store, **common)
    elif args.mode == 'space-default':
        op = secrets.token_hex(16); print(json.dumps({'kind': 'search-default-intent-start', 'operation': op}), flush=True)
        current = observe_space_default(store, **common, expected_revision=args.expected_revision, operation=op, created_at=now)
    elif args.mode == 'pages-default-prepare':
        op = secrets.token_hex(16); print(json.dumps({'kind': 'search-default-intent-start', 'operation': op}), flush=True)
        current = start_pages_default(store, session=session, expected_revision=args.expected_revision, operation=op, created_at=now)
    elif args.mode == 'pages-default-apply':
        print(json.dumps(set_pages_default(store, **common, operation=op), sort_keys=True)); return 0
    elif args.mode == 'pages-default-reconcile': current = reconcile_pages_default(store, **common, operation=op)
    elif args.mode == 'rollback-finish':
        current = finish_rollback(store, session=session, api=api, hub=hub, get=bounded_get)
    elif args.mode == 'abort-unpublished':
        current = tx.abort_publishing(store, _current(store, 'publishing'))
    else: current = finish(store, **common)
    print(json.dumps({'kind': 'search-finalization-step', 'mode': args.mode, 'operation': op,
                      'state': current.record['state'], 'sequence': current.record['sequence'],
                      'external_writers_verified': False}, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__': raise SystemExit(main())
