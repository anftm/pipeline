"""Read-only daily inspection of a staged search candidate and journal state.

This is an evidence handoff, not authorization or automatic promotion. The
staged objects, source revisions, consumers, defaults and durable journal are
independently read; no pointer, site, journal, or configuration is modified.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

try:
    from . import fetch_and_parse as fetch
    from . import search_promotion_preflight as preflight
    from . import search_transition as tx
    from .reader_bucket_store import HubBucketStore
except ImportError:
    import fetch_and_parse as fetch
    import search_promotion_preflight as preflight
    import search_transition as tx
    from reader_bucket_store import HubBucketStore


def inspect(candidate: str, *, hub, session, api, store, get, revision) -> dict:
    before = tx.load(store)
    if before is not None:
        tx.audit_history(store, before)
        if before.record['state'] not in {'complete', 'rolled-back'}:
            raise ValueError('search journal has unfinished work; inspect before staging handoff')
    checked = preflight.inspect(candidate, hub=hub, session=session, hf_api=api, get=get, revision=revision)
    after = tx.load(store)
    if (before is None) != (after is None):
        raise ValueError('search journal changed during candidate handoff')
    pointer = tx.decode_pointer(store.read_pointer())
    if pointer['desired'] != checked['parent'] or pointer['transition'] != checked['parent_transition']:
        raise ValueError('search pointer changed during candidate handoff')
    if before is not None:
        if before.head_raw != after.head_raw or before.etag != after.etag:
            raise ValueError('search journal changed during candidate handoff')
        tx.audit_history(store, after)
        record = after.record
        expected = record['candidate'] if record['state'] == 'complete' else record['parent_pointer']['desired']
        if pointer['desired'] != expected or pointer['desired'] != checked['parent']:
            raise ValueError('search journal terminal generation does not match parent pointer')
        if record['state'] == 'complete' and (pointer['transition'] != record['transition']
                or pointer['previous'] != record['parent_pointer']['desired']):
            raise ValueError('completed journal does not match parent pointer identity')
        if record['state'] == 'rolled-back' and pointer != record['parent_pointer']:
            raise ValueError('rolled-back journal does not match parent pointer identity')
    return {'version': 1, 'kind': 'search-daily-candidate-handoff', 'candidate': candidate,
            'parent': checked['parent'], 'parent_transition': checked['parent_transition'],
            'candidate_records': checked['candidate_records'], 'candidate_objects_verified': checked['candidate_objects_verified'],
            'source_revisions_sha256': checked['source_revisions_sha256'],
            'journal_state': after.record['state'] if after else 'absent',
            'journal_sequence': after.record['sequence'] if after else None,
            'remote_writes': 0, 'pointer_written': False, 'sites_changed': False,
            'publication_ready': False, 'external_writers_verified': False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generation', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    if args.output.exists() or args.output.is_symlink():
        raise ValueError('search handoff output must not already exist')
    import requests
    from huggingface_hub import HfApi
    token = os.environ.get('PAGES_TOKEN', '')
    if not token:
        raise ValueError('existing Pages read credential is required')
    session = requests.Session(); session.headers.update({'Authorization': 'Bearer ' + token,
                                                           'Accept': 'application/vnd.github+json',
                                                           'User-Agent': 'pipeline-search-daily-handoff/1.0'})
    result = inspect(args.generation, hub=HubBucketStore(), session=session,
                     api=HfApi(token=os.environ.get('HF_TOKEN')), store=tx.S3TransitionStore(),
                     get=preflight.daily.bounded_get, revision=fetch.get_repo_sha)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as output:
        json.dump(result, output, ensure_ascii=False, sort_keys=True)
        output.write('\n')
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
