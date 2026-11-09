"""Inspect a durable BHA index request without sending or replaying it.

This reads the exact journal and BHA administrative status only. A missing job,
matching target data, or a successful HTTP response does not establish that a
claimed write did or did not happen. Credentials and payloads are not printed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

try:
    from . import bha_reindex_delivery as delivery
    from . import bha_reindex_journal as journal
    from . import bha_reindex_plan as planning
except ImportError:
    import bha_reindex_delivery as delivery
    import bha_reindex_journal as journal
    import bha_reindex_plan as planning


def inspect(store, token: str, *, read_status=delivery._raw_status) -> dict:
    """Read exact journal before and after the remote status observation."""
    if not token:
        raise ValueError('existing BHA status credential is required')
    before = journal.load(store)
    if before is not None:
        journal.audit(store, before)
    status = read_status(token)
    if not isinstance(status, dict) or not isinstance(status.get('index_status'), str):
        raise ValueError('BHA administrative status is incomplete')
    switch = status.get('switch_pending')
    if type(switch) is not bool:
        raise ValueError('BHA administrative switch status is unknown')
    revisions = status.get('archive_revisions')
    if revisions:
        revisions = planning._trees(revisions, 'consumer revisions')
    elif revisions != {}:
        raise ValueError('BHA administrative revisions are unavailable')
    job = status.get('job')
    if job is not None and not isinstance(job, dict):
        raise ValueError('BHA administrative job status is invalid')
    job_state = job.get('state') if job else None
    if job_state is not None and job_state not in {'accepted', 'running', 'succeeded', 'failed'}:
        raise ValueError('BHA administrative job state is unknown')
    after = journal.load(store)
    if (before is None) != (after is None) or before is not None and (
            before.head_raw != after.head_raw or before.etag != after.etag):
        raise ValueError('BHA journal changed during inspection')
    if after is not None:
        journal.audit(store, after)
    record = after.record if after else None
    exact_job = bool(record and record['job_id'] is not None and job
                     and job.get('job_id') == record['job_id'] and job.get('mode') == record['mode']
                     and job.get('target_archive_revisions') == record['archive_revisions']
                     and job.get('changed_archives') == record['changed_archives'])
    return {'version': 1, 'kind': 'bha-reindex-readonly-inspection',
            'journal': ({'state': record['state'], 'operation': record['operation'],
                         'sequence': record['sequence'], 'mode': record['mode'],
                         'changed_archives': record['changed_archives']} if record else None),
            'index_status': status['index_status'], 'switch_pending': switch,
            'consumer_revisions_complete': bool(revisions), 'job_state': job_state,
            'job_matches_journal': exact_job, 'remote_writes': 0, 'reindex_requested': False,
            'publication_ready': False, 'external_writers_verified': False,
            'scope': 'single journal head and administrative status; not a consumer receipt or retry authorization'}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    if args.output.exists() or args.output.is_symlink():
        raise ValueError('BHA journal report must be a new path')
    token = os.environ.get('BHA_REINDEX_TOKEN', '')
    result = inspect(journal.S3Store(), token)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True); stream.write('\n')
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
