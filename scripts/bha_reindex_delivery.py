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
    from . import bha_consumer_sample as consumer_samples
    from . import bha_snapshot_inspection as snapshots
    from . import check_bha_reindex_access as access
except ImportError:
    import bha_reindex_journal as journal
    import bha_reindex_plan as planning
    import bha_consumer_sample as consumer_samples
    import bha_snapshot_inspection as snapshots
    import check_bha_reindex_access as access


URL = 'https://vomebook-bha-search.hf.space/api/reindex'
MAX_RESPONSE = 128 * 1024


def _status(token: str) -> dict:
    return access.read_status(token)


def _job_matches(job, record, job_id: str) -> bool:
    return (isinstance(job, dict) and job.get('job_id') == job_id and job.get('mode') == record['mode']
            and job.get('target_archive_revisions') == record['archive_revisions']
            and job.get('changed_archives') == record['changed_archives'])


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
    except (urllib.error.URLError, OSError, TimeoutError):
        raise RuntimeError('BHA status request failed') from None
    if len(raw) > access.MAX_BYTES: raise ValueError('BHA status response is oversized')
    value = json.loads(raw)
    if not isinstance(value, dict): raise ValueError('BHA status response is not an object')
    return value


def _post(token: str, *, mode: str, payload: dict) -> dict:
    if not isinstance(token, str) or not token or mode not in {'incremental', 'full'}:
        raise ValueError('existing BHA credential and explicit index mode are required')
    request = urllib.request.Request(URL + ('?full=true' if mode == 'full' else '?full=false'),
                                     data=json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf8'),
                                     method='POST', headers={'Authorization': 'Bearer ' + token,
                                                             'Content-Type': 'application/json',
                                                             'User-Agent': 'pipeline-bha-reindex/1.0'})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            if response.status != 202:
                raise RuntimeError('BHA reindex request result is unconfirmed')
            raw = response.read(MAX_RESPONSE + 1)
    except (urllib.error.URLError, OSError, TimeoutError):
        # Underlying exception strings can contain request details. Never echo
        # them; an unknown result is observed from journal/status, not retried.
        raise RuntimeError('BHA reindex request outcome is unconfirmed; inspect before recovery') from None
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


def reconcile(store, *, operation: str, token: str, status=_raw_status, verify_samples=None,
              verify_snapshot=None, fetch=consumer_samples.get) -> dict:
    """Accept exact job, live target, samples and independently observed snapshot.

    The snapshot verifier must read the exact bucket generation itself. A
    caller-provided fixture or a manifest alone is not production evidence.
    """
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
    if job_id != current.record['job_id'] or not _job_matches(job, current.record, job_id):
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
    if (not isinstance(later, dict) or not _job_matches(later_job, current.record, job_id)
            or later_job.get('state') != 'succeeded'
            or later_job.get('finished_at') != job.get('finished_at')
            or later_job.get('changed') != job.get('changed')
            or later.get('index_status') != 'ready' or later.get('switch_pending') is not False
            or planning._trees(later.get('archive_revisions'), 'consumer trees') != current.record['archive_revisions']):
        raise ValueError('BHA consumer changed during acceptance')
    # The service marks its job succeeded before asynchronous snapshot
    # publication. Do not call that a durable, restart-safe index update.
    if verify_snapshot is None or type(job.get('finished_at')) is not int:
        return {'status': 'snapshot-unverified', 'job_id': job_id, 'journal_advanced': False,
                'external_writers_verified': False}
    # A successful job that did not switch the serving generation cannot be
    # paired to a newly published snapshot by timing alone.
    if job.get('changed') is not True:
        return {'status': 'snapshot-unverified', 'job_id': job_id, 'journal_advanced': False,
                'external_writers_verified': False}
    # Match the post-job snapshot reported by the live service *and* full byte
    # hashes; a same-tree snapshot from an earlier full rebuild is insufficient.
    health = consumer_samples._object(fetch(consumer_samples.BASE + '/api/health',
                                              consumer_samples.MAX_HEALTH), 'health')
    observed_health = consumer_samples.health(json.dumps(health).encode('utf-8'))
    snapshot_status = health.get('bucket_snapshot') if isinstance(health, dict) else None
    if (not isinstance(snapshot_status, dict) or snapshot_status.get('state') != 'published'
            or snapshot_status.get('stage') != 'complete' or not isinstance(snapshot_status.get('snapshot'), str)
            or not snapshots.GEN.fullmatch(snapshot_status['snapshot'])
            or type(snapshot_status.get('updated_at')) is not int or snapshot_status['updated_at'] < job['finished_at']):
        return {'status': 'snapshot-pending', 'job_id': job_id, 'journal_advanced': False,
                'external_writers_verified': False}
    snapshot = verify_snapshot(current.record['archive_revisions'], full_sidecars=True,
                               expected_generation=snapshot_status['snapshot'], not_before=job['finished_at'])
    if (not isinstance(snapshot, dict) or snapshot.get('kind') != 'bha-durable-snapshot-observation'
            or snapshot.get('generation') != snapshot_status['snapshot']
            or snapshot.get('manifest_verified') is not True or snapshot.get('sidecars_verified') is not True
            or snapshot.get('archive_revisions') != current.record['archive_revisions']
            or snapshot.get('remote_writes') != 0 or snapshot.get('external_writers_verified') is not False
            or not isinstance(snapshot.get('generation'), str) or not snapshot['generation']
            or not isinstance(snapshot.get('manifest_sha256'), str)
            or not journal.SHA.fullmatch(snapshot['manifest_sha256'])):
        raise ValueError('BHA snapshot evidence is missing, incomplete or differs from target')
    # The hashes do not verify an Elasticsearch restore. Preserve that limit in
    # the terminal result rather than treating a snapshot as a restore test.
    if (snapshot.get('restore_verified') is not False
            or snapshot.get('document_count') != observed_health['document_count']
            or snapshot.get('build_fingerprint') != later.get('build_fingerprint')):
        raise ValueError('BHA snapshot evidence has an unsupported restore or serving identity claim')
    health_after = consumer_samples._object(fetch(consumer_samples.BASE + '/api/health',
                                                    consumer_samples.MAX_HEALTH), 'health')
    after_health = consumer_samples.health(json.dumps(health_after).encode('utf-8'))
    after_snapshot = health_after.get('bucket_snapshot') if isinstance(health_after, dict) else None
    if (after_health != observed_health or not isinstance(after_snapshot, dict) or after_snapshot.get('state') != 'published'
            or after_snapshot.get('stage') != 'complete'
            or after_snapshot.get('snapshot') != snapshot_status['snapshot']):
        raise ValueError('BHA snapshot status changed during acceptance')
    final = status(token)
    final_job = final.get('job') if isinstance(final, dict) else None
    if (not isinstance(final, dict) or not _job_matches(final_job, current.record, job_id)
            or final_job.get('state') != 'succeeded'
            or final_job.get('finished_at') != job.get('finished_at')
            or final_job.get('changed') != job.get('changed')
            or final.get('build_fingerprint') != later.get('build_fingerprint')
            or final.get('index_status') != 'ready' or final.get('switch_pending') is not False
            or planning._trees(final.get('archive_revisions'), 'consumer trees') != current.record['archive_revisions']):
        raise ValueError('BHA consumer changed during snapshot acceptance')
    result = {'job_id': job_id, 'state': 'succeeded', 'consumer_revisions_verified': True,
              'samples': sample_report['samples'], 'sample_count': len(sample_report['samples']),
              'snapshot_generation': snapshot['generation'], 'snapshot_manifest_sha256': snapshot['manifest_sha256'],
              'snapshot_sidecars_verified': True, 'snapshot_restore_verified': False}
    terminal = journal.terminal(store, current, state='verified', result=result)
    return {'status': 'verified', 'job_id': job_id, 'sequence': terminal.record['sequence'],
            'journal_advanced': True, 'external_writers_verified': False}
