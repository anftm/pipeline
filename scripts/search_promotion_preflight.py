"""Independent, read-only preflight for an already-staged search generation.

This never writes a pointer, changes either site, or proves writers are fenced.
The candidate may only proceed after its own object/source checks and the
parent's independently observed consumers and defaults all agree.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path

try:
    from . import fetch_and_parse as fetch
    from . import search_adopt as adoption
    from . import search_daily as daily
    from . import search_default_receipts as defaults
    from . import search_snapshot
    from .reader_bucket_store import HubBucketStore
    from .search_publication_pointer import BUCKET, decode_pointer
except ImportError:
    import fetch_and_parse as fetch
    import search_adopt as adoption
    import search_daily as daily
    import search_default_receipts as defaults
    import search_snapshot
    from reader_bucket_store import HubBucketStore
    from search_publication_pointer import BUCKET, decode_pointer


REV = re.compile(r'[0-9a-f]{40}\Z')


def inspect(candidate: str, *, hub, session, hf_api, get=daily.bounded_get, revision=fetch.get_repo_sha,
            allow_current: bool = False) -> dict:
    pointer_raw = hub.read_bytes(BUCKET, 'published/current.json')
    parent = decode_pointer(pointer_raw)
    already_current = parent['desired'] == candidate
    if already_current and not allow_current:
        raise ValueError('search candidate is already the desired generation')
    expected_parent = daily.inspect_published(get=get)
    if expected_parent['generation'] != parent['desired'] or expected_parent['transition'] != parent['transition']:
        raise ValueError('search desired pointer changed during parent observation')
    manifest = adoption.validate_generation(hub, candidate)
    captured_manifest = hub.read_bytes(BUCKET, f'generations/{candidate}/manifest.json')
    if not isinstance(captured_manifest, bytes) or json.loads(captured_manifest) != manifest:
        raise ValueError('candidate manifest changed during object verification')
    revisions = manifest.get('source_revisions')
    if (not isinstance(revisions, dict) or set(revisions) != set(fetch.REPOS)
            or any(not isinstance(value, str) or not REV.fullmatch(value) for value in revisions.values())):
        raise ValueError('candidate source revisions are incomplete or malformed')
    token = os.environ.get('HF_TOKEN', '')
    before = {repo: revision(repo, token) for repo in fetch.REPOS}
    if before != revisions:
        raise ValueError('search source revisions differ from candidate')
    pages_default = json.loads(defaults.observe_pages(parent['desired'], session=session))
    space_default = json.loads(defaults.observe_space(parent['desired'], api=hf_api))
    after = {repo: revision(repo, token) for repo in fetch.REPOS}
    if after != before:
        raise ValueError('search source revisions changed during inspection')
    if hub.read_bytes(BUCKET, 'published/current.json') != pointer_raw:
        raise ValueError('search pointer changed during preflight')
    if hub.read_bytes(BUCKET, f'generations/{candidate}/manifest.json') != captured_manifest:
        raise ValueError('candidate manifest changed during preflight')
    final_parent = daily.inspect_published(get=get)
    if final_parent != expected_parent:
        raise ValueError('search consumer evidence changed during preflight')
    if (json.loads(defaults.observe_pages(parent['desired'], session=session))['generation'] != pages_default['generation']
            or json.loads(defaults.observe_space(parent['desired'], api=hf_api))['generation'] != space_default['generation']):
        raise ValueError('search consumer defaults changed during preflight')
    return {'version': 1, 'kind': 'search-promotion-readonly-preflight',
            'parent': parent['desired'], 'parent_transition': parent['transition'], 'candidate': candidate,
            'candidate_already_current': already_current,
            'candidate_records': manifest['record_count'], 'source_revisions': revisions,
            'source_revisions_sha256': hashlib.sha256(search_snapshot.canonical(revisions)).hexdigest(),
            'candidate_objects_verified': sum(len(entries) for entries in manifest['objects'].values()),
            'pages_default_generation': pages_default['generation'], 'space_default_generation': space_default['generation'],
            'parent_consumers_observed_twice': True, 'remote_writes': 0, 'pointer_written': False,
            'publication_ready': False, 'external_writers_verified': False,
            'scope': 'candidate bytes and source heads; parent consumers/defaults only, not an activation receipt'}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generation', required=True)
    parser.add_argument('--allow-current', action='store_true', help='read-only verification of the current generation')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    if args.output.exists() or args.output.is_symlink():
        raise ValueError('search preflight output must not already exist')
    import requests
    from huggingface_hub import HfApi
    token = os.environ.get('PAGES_TOKEN', '')
    if not token:
        raise ValueError('existing Pages read credential is required')
    session = requests.Session(); session.headers.update({'Authorization': 'Bearer ' + token,
                                                           'Accept': 'application/vnd.github+json',
                                                           'User-Agent': 'pipeline-search-promotion-preflight/1.0'})
    result = inspect(args.generation, hub=HubBucketStore(), session=session,
                     hf_api=HfApi(token=os.environ.get('HF_TOKEN')), allow_current=args.allow_current)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as output:
        json.dump(result, output, ensure_ascii=False, sort_keys=True)
        output.write('\n')
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
