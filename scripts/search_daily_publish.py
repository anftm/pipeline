"""Start one daily publication using the existing durable transition journal.

Start-only: any unfinished journal, including this candidate from an earlier
process, requires explicit operator reconciliation. The caller must serialize
the entire run and coordinate other writers. No local receipt is recovery state.
The timeout bounds orchestration/polling, not an in-flight adapter HTTP call.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import time
from datetime import UTC, datetime

import requests

try:
    from . import search_daily as daily, search_daily_journal as daily_journal
    from . import search_promotion_intent as promotion_intent
    from . import search_space_transition as space, search_pages_transition as pages
    from . import search_finalize_transition as finalize, search_transition as tx
    from . import search_snapshot
    from .search_publication_pointer import S3SearchPointerStore, make_pointer, _generation
except ImportError:
    import search_daily as daily, search_daily_journal as daily_journal
    import search_promotion_intent as promotion_intent
    import search_space_transition as space, search_pages_transition as pages
    import search_finalize_transition as finalize, search_transition as tx
    import search_snapshot
    from search_publication_pointer import S3SearchPointerStore, make_pointer, _generation


class Blocked(RuntimeError):
    """No observed progress before the deadline; retain the journal for inspection."""


def _now():
    return datetime.now(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _emit(event):
    print(json.dumps(event, ensure_ascii=False, sort_keys=True), flush=True)


# Only write calls use this classification. Read/validation failures propagate;
# an uncertain write is observed, never retried, even with a consumed claim.
UNKNOWN_WRITE = (OSError, requests.RequestException, RuntimeError)
WAIT_STAGES = {'RUNNING_BUILDING', 'BUILDING', 'APP_STARTING'}


def publish(*, generation, expected_parent, pages_revision, space_revision,
            known_writers_coordinated, timeout_seconds, store, pointer_store, hub,
            session, api, get=daily.bounded_get, revision=None,
            clock=time.monotonic, sleep=time.sleep, emit=_emit):
    """Run the normal path once; injected adapters/clocks support offline tests."""
    _generation(generation)
    _generation(expected_parent)
    if generation == expected_parent:
        raise ValueError('candidate must differ from expected parent')
    if not pages.REV.fullmatch(pages_revision) or not space.SHA.fullmatch(space_revision):
        raise ValueError('both site revisions must be exact lowercase 40-character SHAs')
    if not known_writers_coordinated:
        raise ValueError('known writer coordination must be explicitly declared')
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError('timeout must be finite and positive')
    revision = revision or promotion_intent.preflight.fetch.get_repo_sha
    deadline = clock() + timeout_seconds
    transaction = None
    parent_raw = None
    last = 'preflight'
    common = dict(hub=hub, api=api, session=session, get=get, revision=revision)

    def budget():
        if clock() >= deadline:
            raise Blocked('daily publication timed out: ' + last + '; journal retained, do not replay writes')

    def wait(status):
        nonlocal last
        last = status
        emit({'kind': 'search-daily-publish-wait', 'transaction': transaction, 'status': status})
        budget()
        sleep(min(5, max(0, deadline - clock())))
        budget()

    def current():
        value = tx.load(store)
        if (value is None or value.record['transition'] != transaction
                or value.record['candidate'] != generation
                or search_snapshot.canonical(value.record['parent_pointer']) != parent_raw):
            raise ValueError('daily publication journal identity changed')
        tx.audit_history(store, value)
        pending = value.record['pending']
        if pending and pending['expected_revision'] != {'pages': pages_revision, 'space': space_revision}[pending['site']]:
            raise ValueError('pending operation differs from bound site revision')
        intended = make_pointer(generation, expected_parent, transaction)
        allowed = {parent_raw}
        if value.record['state'] == 'publishing':
            allowed.add(intended)
        elif value.record['state'] in {'pointer-observed', 'complete'}:
            allowed = {intended}
        if store.read_pointer() not in allowed:
            raise ValueError('daily publication exact pointer identity changed')
        return value

    def identities():
        """Wait only for recognized runtime stages; never accept them as loaded."""
        while True:
            budget()
            value = current() if transaction is not None else None
            if pages._main(session) != pages_revision:
                raise ValueError('Pages revision differs from bound revision')
            if api.space_info(space.deploy.SPACE, timeout=20).sha != space_revision:
                raise ValueError('Space repository differs from bound revision')
            runtime = api.get_space_runtime(space.deploy.SPACE)
            raw = getattr(runtime, 'raw', None)
            sha = raw.get('sha') if isinstance(raw, dict) else None
            if sha is not None and sha != space_revision:
                raise ValueError('Space runtime differs from bound revision')
            if runtime.stage == 'RUNNING':
                space.deploy.runtime(api, expected_sha=space_revision)
                budget()
                return
            if runtime.stage not in WAIT_STAGES:
                raise ValueError('Space runtime is unavailable: ' + str(runtime.stage))
            if value is not None:
                configured, updated = space.deploy.variable(api)
                pending = value.record['pending']
                allowed = {value.record['observed']['space']['generation']}
                if pending and pending['site'] == 'space' and pending['operation'] == 'apply':
                    allowed.add(generation)
                    if space.deploy._timestamp(updated) < space.deploy._timestamp(pending['config_before']['updated_at']):
                        raise ValueError('Space configuration timestamp moved backwards')
                if configured not in allowed:
                    raise ValueError('Space configuration differs while waiting')
            wait('space-' + runtime.stage)

    def step(function, **kwargs):
        identities()
        result = function(store, **kwargs)
        current()
        return result

    def write_once(function, *, claim_operation, **kwargs):
        identities()
        pending = current().record['pending']
        if not pending or pending['operation_id'] != claim_operation:
            raise ValueError('no exact daily publication operation')
        # This run may have observed a consumed claim. It cannot send again.
        if pending.get('write_claim') is not None or 'write_claim' not in pending:
            return
        try:
            function(store, **kwargs)
        except UNKNOWN_WRITE as error:
            pending = current().record['pending']
            if not pending or pending['operation_id'] != claim_operation or not pending.get('write_claim'):
                raise
            emit({'kind': 'search-daily-publish-unknown', 'transaction': transaction,
                  'operation': claim_operation, 'error_type': type(error).__name__})
        current()

    def await_site(site, operation, reconcile, **kwargs):
        before = current().record['sequence']
        while True:
            identities()
            result = reconcile(store, operation_id=operation, **kwargs)
            value = current().record
            if (value['pending'] is None and value['sequence'] > before
                    and value['observed'][site]['generation'] == generation):
                return
            pending = value['pending']
            if not pending or pending['site'] != site or pending['operation_id'] != operation:
                raise ValueError('site reconciliation changed operation identity without acceptance')
            allowed = {'parent-configured', 'building'} if site == 'space' else {'run-not-visible', 'deploying'}
            if result.get('status') not in allowed or result.get('journal_advanced'):
                raise ValueError('site reconciliation did not durably accept the candidate')
            wait(site + '-' + result['status'])

    # A terminal, audited parent is mandatory. This is deliberately not resume.
    baseline = daily_journal.inspect(store)
    if baseline['generation'] != expected_parent:
        raise ValueError('terminal journal differs from expected parent')
    parent_raw = store.read_pointer()
    identities()
    daily_journal.verify_unchanged(baseline, daily_journal.inspect(store))
    operation = secrets.token_hex(16)
    promotion_intent.prepare(generation, store=store, transaction_id=operation, **common)
    transaction = operation
    if current().record['state'] != 'prepared':
        raise ValueError('new daily publication is not prepared')
    emit({'kind': 'search-daily-publish-start', 'transaction': transaction, 'candidate': generation,
          'expected_parent': expected_parent, 'pages_revision': pages_revision, 'space_revision': space_revision,
          'resume_supported': False, 'external_writers_verified': False})

    operation = secrets.token_hex(16)
    step(space.prepare, api=api, expected_revision=space_revision, operation_id=operation, created_at=_now())
    write_once(space.apply_once, claim_operation=operation, api=api, hub=hub, operation_id=operation)
    await_site('space', operation, space.reconcile, api=api, hub=hub, get=get)

    operation = secrets.token_hex(16)
    site_common = dict(session=session, api=api, hub=hub, get=get)
    step(pages.prepare, **site_common, expected_revision=pages_revision, operation_id=operation, created_at=_now())
    write_once(pages.apply_once, claim_operation=operation, operation_id=operation, **site_common)
    await_site('pages', operation, pages.reconcile, **site_common)

    step(finalize.mark_publishing, **common)
    identities()
    try:
        finalize.publish(store, pointer_store, **common)
    except UNKNOWN_WRITE as error:
        emit({'kind': 'search-daily-publish-unknown', 'transaction': transaction,
              'status': 'pointer-readback-required', 'error_type': type(error).__name__})
    # Never call publish again, even if the exact parent remains visible.
    intended = make_pointer(generation, expected_parent, transaction)
    while True:
        identities()
        value = current()
        if value.record['state'] == 'pointer-observed':
            break
        if value.record['state'] != 'publishing':
            raise ValueError('unexpected journal state after pointer publication')
        if store.read_pointer() == intended:
            step(finalize.reconcile_pointer, **common)
        else:
            wait('pointer-parent-visible')

    step(finalize.observe_space_default, **common, expected_revision=space_revision,
         operation=secrets.token_hex(16), created_at=_now())
    operation = secrets.token_hex(16)
    step(finalize.start_pages_default, session=session, expected_revision=pages_revision,
         operation=operation, created_at=_now())
    write_once(finalize.set_pages_default, claim_operation=operation, **common, operation=operation)
    while True:
        identities()
        # Observe the parent as a valid wait condition without suppressing an
        # invalid response, a foreign default, or a consumer validation error.
        response = session.get(f'{pages.deploy.BASE}/actions/variables/{finalize.VARIABLE}', timeout=20)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or body.get('name') != finalize.VARIABLE:
            raise ValueError('invalid Pages default readback')
        if body.get('value') == expected_parent:
            wait('pages-default-parent-visible')
            continue
        if body.get('value') != generation:
            raise ValueError('Pages default readback has a foreign generation')
        before = current().record['sequence']
        step(finalize.reconcile_pages_default, **common, operation=operation)
        value = current().record
        if (value['pending'] is not None or value['sequence'] <= before
                or value['defaults']['pages']['generation'] != generation):
            raise ValueError('Pages default was not durably observed')
        break
    step(finalize.finish, **common)
    identities()
    published = daily.inspect_published(get=get)
    journal = daily_journal.inspect(store)
    if (published['generation'] != generation or published['transition'] != transaction
            or journal['state'] != 'complete' or journal['generation'] != generation
            or journal['transition'] != transaction or journal['pointer_transition'] != transaction):
        raise ValueError('fresh daily inspections do not confirm this completed candidate')
    identities()
    return {'kind': 'search-daily-publish-complete', 'generation': generation, 'transaction': transaction,
            'state': 'complete', 'published': published, 'journal': journal,
            'resume_supported': False, 'external_writers_verified': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('generation', 'expected-parent', 'pages-revision', 'space-revision'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--known-writers-coordinated', action='store_true')
    parser.add_argument('--timeout-seconds', type=float, default=1800)
    args = parser.parse_args(argv)
    token = os.environ.get('PAGES_TOKEN', '')
    if not token:
        raise ValueError('existing Pages credential is required')
    from huggingface_hub import HfApi
    session = requests.Session()
    session.headers.update({'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                            'User-Agent': 'pipeline-search-daily-publish/1.0'})
    try:
        result = publish(**vars(args), store=tx.S3TransitionStore(), pointer_store=S3SearchPointerStore(),
                         hub=space.HubBucketStore(), session=session,
                         api=HfApi(token=os.environ.get('HF_TOKEN')))
    except Blocked as error:
        _emit({'kind': 'search-daily-publish-blocked', 'status': 'blocked', 'reason': str(error),
               'journal_retained': True, 'resume_supported': False})
        return 2
    _emit(result)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
