#!/usr/bin/env python3
"""Compare one BHA parsed commit/tree to the recorded baseline, without writes."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re

try:
    from . import check_bha_revisions as revisions, check_bha_reindex_access as access
except ImportError:
    import check_bha_revisions as revisions
    import check_bha_reindex_access as access


def inspect(token, archive_id: int, state_path: Path, lookup=revisions.archive_identity):
    if not token:
        raise RuntimeError('GH_PAT is required')
    if not 0 <= archive_id <= 31:
        raise ValueError('archive number must be between 0 and 31')
    state = json.loads(state_path.read_bytes())
    if not isinstance(state, dict):
        raise ValueError('invalid parsed revision baseline')
    key = str(archive_id)
    previous = state.get(key)
    if not isinstance(previous, str) or not re.fullmatch(r'[0-9a-f]{40}', previous):
        raise ValueError('parsed revision baseline is missing or malformed')
    observed_key, commit, tree = lookup(token, archive_id)
    if (observed_key != key or not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit)
            or not isinstance(tree, str) or not re.fullmatch(r'[0-9a-f]{40}', tree)):
        raise ValueError('invalid parsed revision observation')
    return {'version': 1, 'kind': 'bha-parsed-readonly-report', 'archive': archive_id,
            'commit': commit, 'tree': tree, 'baseline_tree': previous,
            'tree_changed': tree != previous, 'remote_writes': 0, 'reindex_requested': False,
            'scope': 'GitHub parsed commit/tree compared to repository state; BHA consumer not checked'}


def inspect_all(token, reindex_token, state_path: Path, lookup=revisions.archive_identity,
                read_status=access.read_status):
    """Compare all exact parsed trees to a stable authenticated consumer view."""
    if not token or not reindex_token:
        raise RuntimeError('both GitHub and BHA read-only credentials are required')
    state = json.loads(state_path.read_bytes())
    expected = {str(number) for number in range(32)}
    def valid(value):
        return (isinstance(value, dict) and set(value) == expected and
                all(isinstance(tree, str) and re.fullmatch(r'[0-9a-f]{40}', tree) for tree in value.values()))
    def consumer():
        status = read_status(reindex_token)
        if (not isinstance(status, dict) or status.get('index_status') != 'ready'
                or status.get('switch_pending') is not False or not valid(status.get('archive_revisions'))):
            raise ValueError('BHA consumer must be ready with all 32 exact trees')
        return dict(status['archive_revisions'])
    if not valid(state):
        raise ValueError('parsed revision baseline must contain all 32 exact trees')
    before = consumer()
    with ThreadPoolExecutor(max_workers=4) as pool:
        observed = list(pool.map(lambda number: lookup(token, number), range(32)))
    if any(not isinstance(item, tuple) or len(item) != 3 or item[0] != str(number)
           or not isinstance(item[1], str) or not re.fullmatch(r'[0-9a-f]{40}', item[1])
           or not isinstance(item[2], str) or not re.fullmatch(r'[0-9a-f]{40}', item[2])
           for number, item in enumerate(observed)):
        raise ValueError('invalid parsed revision observation')
    after = consumer()
    if after != before:
        raise RuntimeError('BHA consumer revisions changed during inspection')
    trees = {key: tree for key, _, tree in observed}
    rows = [{'archive': int(key), 'commit': commit, 'tree': tree, 'baseline_tree': state[key],
             'consumer_tree': before[key], 'changed_from_baseline': tree != state[key],
             'consumer_matches_tree': tree == before[key]} for key, commit, tree in observed]
    return {'version': 1, 'kind': 'bha-parsed-consumer-readonly-report', 'archives': rows,
            'changed_from_baseline': [int(key) for key in sorted(expected, key=int) if trees[key] != state[key]],
            'consumer_differences': [int(key) for key in sorted(expected, key=int) if trees[key] != before[key]],
            'consumer_ready_observed_twice': True, 'remote_writes': 0, 'reindex_requested': False,
            'scope': 'GitHub trees and stable reported consumer revisions; loaded bytes and later changes not verified'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, help='one archive number or all')
    parser.add_argument('--state', type=Path, default=Path('state/bha-parsed-revisions.json'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.resolve() == args.state.resolve():
        raise ValueError('report cannot overwrite the revision baseline')
    token = os.environ.get('GH_PAT') or os.environ.get('GITHUB_TOKEN', '')
    if args.archive == 'all':
        report = inspect_all(token, os.environ.get('BHA_REINDEX_TOKEN', ''), args.state)
    else:
        try:
            archive = int(args.archive)
        except ValueError:
            parser.error('archive must be all or an integer from 0 to 31')
        report = inspect(token, archive, args.state)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
