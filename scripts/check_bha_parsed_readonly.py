#!/usr/bin/env python3
"""Compare one BHA parsed commit/tree to the recorded baseline, without writes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re

try:
    from . import check_bha_revisions as revisions
except ImportError:
    import check_bha_revisions as revisions


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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=int, required=True)
    parser.add_argument('--state', type=Path, default=Path('state/bha-parsed-revisions.json'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.resolve() == args.state.resolve():
        raise ValueError('report cannot overwrite the revision baseline')
    report = inspect(os.environ.get('GH_PAT') or os.environ.get('GITHUB_TOKEN', ''), args.archive, args.state)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
