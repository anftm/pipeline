"""Create a durable search transition from a verified candidate, without deployment.

A successful journal write is not a site rollout. Source revisions, every
immutable candidate object, both actual parent consumers and both configured
defaults are read before the transition head is changed. Known writers must be
coordinated independently; no single-object CAS is a global fence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets

try:
    from . import search_adopt as adoption
    from . import search_consumer_receipts as receipts
    from . import search_default_receipts as defaults
    from . import search_promotion_preflight as preflight
    from . import search_snapshot
    from . import search_transition as transition
    from .search_publication_pointer import BUCKET, decode_pointer
except ImportError:
    import search_adopt as adoption
    import search_consumer_receipts as receipts
    import search_default_receipts as defaults
    import search_promotion_preflight as preflight
    import search_snapshot
    import search_transition as transition
    from search_publication_pointer import BUCKET, decode_pointer


def prepare(candidate: str, *, hub, session, api, store, get, revision, transaction_id: str) -> dict:
    check = preflight.inspect(candidate, hub=hub, session=session, hf_api=api, get=get, revision=revision)
    raw = hub.read_bytes(BUCKET, 'published/current.json'); parent = decode_pointer(raw)
    if parent['desired'] != check['parent'] or parent['transition'] != check['parent_transition']:
        raise ValueError('search parent pointer changed after candidate preflight')
    parent_manifest = adoption.validate_generation(hub, parent['desired'])
    pages = receipts.observe('pages', parent['desired'], parent_manifest, get=get)
    space = receipts.observe('space', parent['desired'], parent_manifest, get=get)
    pages_default = defaults.observe_pages(parent['desired'], session=session)
    space_default = defaults.observe_space(parent['desired'], api=api)
    final = preflight.inspect(candidate, hub=hub, session=session, hf_api=api, get=get, revision=revision)
    if check != final or hub.read_bytes(BUCKET, 'published/current.json') != raw:
        raise ValueError('search candidate or parent changed before durable intent')
    expected_source = hashlib.sha256(search_snapshot.canonical(check['source_revisions'])).hexdigest()
    if expected_source != check['source_revisions_sha256']:
        raise ValueError('search source revision identity differs')
    current = transition.begin(store, parent_pointer=raw, candidate=candidate, source_revisions_sha256=expected_source,
                               pages_receipt=pages, space_receipt=space, pages_default=pages_default,
                               space_default=space_default, transition=transaction_id)
    return {'version': 1, 'kind': 'search-promotion-intent-receipt', 'transaction': current.record['transition'],
            'parent': parent['desired'], 'candidate': candidate, 'sequence': current.record['sequence'],
            'state': current.record['state'], 'journal_written': True, 'pointer_written': False,
            'sites_changed': False, 'publication_ready': False, 'external_writers_verified': False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generation', required=True)
    parser.add_argument('--known-writers-coordinated', action='store_true')
    args = parser.parse_args(argv)
    if not args.known_writers_coordinated:
        raise ValueError('known writer coordination must be explicitly declared')
    import requests
    from huggingface_hub import HfApi
    try:
        from .reader_bucket_store import HubBucketStore
    except ImportError:
        from reader_bucket_store import HubBucketStore
    token = os.environ.get('PAGES_TOKEN', '')
    if not token:
        raise ValueError('existing Pages read credential is required')
    session = requests.Session(); session.headers.update({'Authorization': 'Bearer ' + token,
                                                           'Accept': 'application/vnd.github+json',
                                                           'User-Agent': 'pipeline-search-promotion-intent/1.0'})
    op = secrets.token_hex(16)
    print(json.dumps({'kind': 'search-promotion-intent-start', 'transaction': op,
                      'candidate': args.generation, 'known_writers_declared_coordinated': True,
                      'external_writers_verified': False}, sort_keys=True), flush=True)
    result = prepare(args.generation, hub=HubBucketStore(), session=session,
                     api=HfApi(token=os.environ.get('HF_TOKEN')), store=transition.S3TransitionStore(),
                     get=preflight.daily.bounded_get, revision=preflight.fetch.get_repo_sha, transaction_id=op)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
