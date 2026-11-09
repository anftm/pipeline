"""Prepare, send once, and independently reconcile a BHA index request.

The caller must coordinate known index writers. A durable claim and current
status are not a global fence. If the POST response is lost and no exact job
is visible, the request remains claimed; this module never retries it.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

try:
    from . import bha_reindex_journal as journal
    from . import bha_reindex_plan as planning
    from . import check_bha_reindex_access as access
except ImportError:
    import bha_reindex_journal as journal
    import bha_reindex_plan as planning
    import check_bha_reindex_access as access


URL = 'https://vomebook-bha-search.hf.space/api/reindex'
MAX_RESPONSE = 128 * 1024


def _status(token: str) -> dict:
    return access.read_status(token)


def _raw_status(token: str) -> dict:
    """Read status during indexing without treating rebuilding as failure."""
    if not token:
        raise ValueError('existing BHA status credential is required')
    request = urllib.request.Request(access.URL, headers={'Authorization': 'Bearer ' + token,
                                                            'Accept': 'application/json',
                                                            'User-Agent': 'pipeline-bha-reindex-reconcile/1.0'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 200: raise ValueError('BHA status HTTP response differs')
            raw = response.read(access.MAX_BYTES + 1)
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise RuntimeError('BHA status request failed') from error
    if len(raw) > access.MAX_BYTES: raise ValueError('BHA status response is oversized')
    value = json.loads(raw)
    if not isinstance(value, dict): raise ValueError('BHA status response is not an object')
    return value


def _post(token: str, *, mode: str, payload: dict) -> dict:
    request = urllib.request.Request(URL + ('?full=true' if mode == 'full' else '?full=false'),
                                     data=json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf8'),
                                     method='POST', headers={'Authorization': 'Bearer ' + token,
                                                             'Content-Type': 'application/json',
                                                             'User-Agent': 'pipeline-bha-reindex/1.0'})
    with urllib.request.urlopen(request, timeout=45) as response:
        if response.status != 202:
            raise RuntimeError('BHA reindex request result is unconfirmed')
        raw = response.read(MAX_RESPONSE + 1)
    if len(raw) > MAX_RESPONSE:
        raise ValueError('BHA reindex response is oversized')
    value = json.loads(raw)
    if not isinstance(value, dict): raise ValueError('BHA reindex response is invalid')
    return value


def _plan_current(current: dict, expected: dict) -> None:
    keys = ('archive_commits', 'archive_revisions', 'consumer_revisions', 'changed_archives', 'consumer_fingerprint', 'last_job')
    if any(current.get(key) != expected.get(key) for key in keys):
        raise ValueError('BHA sources or consumer changed before index request')


def prepare(store, *, plan: dict, mode: str, inspect_live, operation: str | None = None) -> journal.Current:
    fresh = inspect_live()
    _plan_current(fresh, plan)
    if mode not in {'incremental', 'full'} or mode == 'incremental' and not fresh['changed_archives']:
        raise ValueError('BHA index mode has no applicable change')
    return journal.begin(store, fresh, mode=mode, operation=operation)


def apply_once(store, *, operation: str, token: str, inspect_live, post=_post) -> dict:
    current = journal.load(store)
    if current is None or current.record['operation'] != operation or current.record['state'] != 'prepared':
        raise ValueError('BHA request is not in exact prepared state')
    journal.audit(store, current)
    fresh = inspect_live()
    expected = {'archive_commits': current.record['archive_commits'], 'archive_revisions': current.record['archive_revisions'],
                'consumer_revisions': current.record['baseline_revisions'], 'changed_archives': current.record['changed_archives'],
                'consumer_fingerprint': current.record['baseline_fingerprint'], 'last_job': current.record['baseline_job']}
    if any(fresh.get(k) != v for k, v in expected.items()):
        raise ValueError('BHA live source or consumer changed before request')
    claimed = journal.claim(store, current)
    journal.check_claim(store, claimed)
    payload = {'archive_commits': current.record['archive_commits'], 'archive_revisions': current.record['archive_revisions'],
               'changed_archives': current.record['changed_archives']}
    # One POST only. A timeout may have enqueued the job; reconciliation reads
    # actual status and never sends this request again.
    result = post(token, mode=current.record['mode'], payload=payload)
    if (result.get('ok') is not True or result.get('status') != 'accepted'
            or result.get('mode') != current.record['mode']):
        raise ValueError('BHA response does not confirm the requested mode')
    updated = journal.accepted(store, claimed, job_id=result.get('job_id'))
    return {'kind': 'bha-reindex-request', 'operation': operation, 'job_id': updated.record['job_id'],
            'state': updated.record['state'], 'consumer_verified': False, 'external_writers_verified': False}


def reconcile(store, *, operation: str, token: str, status=_raw_status, verify_samples=None) -> dict:
    """Accept only the exact job and independently observed target revisions."""
    current = journal.load(store)
    if current is None or current.record['operation'] != operation or current.record['state'] not in {'claimed', 'accepted'}:
        raise ValueError('BHA request is not pending reconciliation')
    journal.audit(store, current)
    data = status(token)
    job = data.get('job') if isinstance(data, dict) else None
    if not isinstance(job, dict):
        return {'status': 'job-unknown', 'journal_advanced': False, 'external_writers_verified': False}
    job_id = job.get('job_id')
    if current.record['state'] == 'claimed':
        # No request ID is sent by the current BHA API. Even identical targets
        # could belong to another writer; do not adopt an uncorrelated job.
        return {'status': 'job-uncorrelated', 'journal_advanced': False, 'external_writers_verified': False}
    if (job_id != current.record['job_id'] or job.get('mode') != current.record['mode']
            or job.get('target_archive_revisions') != current.record['archive_revisions']
            or job.get('changed_archives') != current.record['changed_archives']):
        raise ValueError('BHA status job does not match the accepted job and targets')
    if job.get('state') in {'accepted', 'running'}:
        return {'status': 'running', 'job_id': job_id, 'journal_advanced': False, 'external_writers_verified': False}
    if job.get('state') == 'failed':
        # A failed job is recorded but does not authorize automatic retry.
        result = {'job_id': job_id, 'state': 'failed', 'consumer_ready': data.get('index_status') == 'ready'}
        terminal = journal.terminal(store, current, state='failed', result=result)
        return {'status': 'failed', 'job_id': job_id, 'sequence': terminal.record['sequence'],
                'journal_advanced': True, 'external_writers_verified': False}
    if (job.get('state') != 'succeeded' or data.get('index_status') != 'ready' or data.get('switch_pending') is not False
            or planning._trees(data.get('archive_revisions'), 'consumer trees') != current.record['archive_revisions']):
        raise ValueError('BHA job has not produced the exact ready target revisions')
    if verify_samples is None:
        raise ValueError('BHA actual consumer sample verification is required')
    sample_report = verify_samples()
    if (not isinstance(sample_report, dict) or sample_report.get('consumer_health_observed_twice') is not True
            or sample_report.get('remote_writes') != 0 or not isinstance(sample_report.get('samples'), list)
            or not sample_report['samples']):
        raise ValueError('BHA consumer sample verification is incomplete')
    for row in sample_report['samples']:
        key = str(row.get('archive')) if isinstance(row, dict) else ''
        if (key not in current.record['archive_commits'] or row.get('commit') != current.record['archive_commits'][key]
                or row.get('tree') != current.record['archive_revisions'][key]
                or row.get('preview_article_equal') is not True or row.get('exact_title_search_found') is not True):
            raise ValueError('BHA consumer sample does not match target revision')
    observed_archives = {row['archive'] for row in sample_report['samples']}
    if (current.record['mode'] == 'incremental'
            and not set(current.record['changed_archives']).issubset(observed_archives)):
        raise ValueError('BHA consumer samples do not cover all changed archives')
    later = status(token)
    later_job = later.get('job') if isinstance(later, dict) else None
    if (not isinstance(later, dict) or not isinstance(later_job, dict)
            or later_job.get('job_id') != job_id or later_job.get('state') != 'succeeded'
            or later_job.get('mode') != current.record['mode']
            or later_job.get('target_archive_revisions') != current.record['archive_revisions']
            or later_job.get('changed_archives') != current.record['changed_archives']
            or later.get('index_status') != 'ready' or later.get('switch_pending') is not False
            or planning._trees(later.get('archive_revisions'), 'consumer trees') != current.record['archive_revisions']):
        raise ValueError('BHA consumer changed during acceptance')
    result = {'job_id': job_id, 'state': 'succeeded', 'consumer_revisions_verified': True,
              'samples': sample_report['samples'], 'sample_count': len(sample_report['samples'])}
    terminal = journal.terminal(store, current, state='verified', result=result)
    return {'status': 'verified', 'job_id': job_id, 'sequence': terminal.record['sequence'],
            'journal_advanced': True, 'external_writers_verified': False}
