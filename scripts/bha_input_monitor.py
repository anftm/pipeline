"""Compare pinned BHA parser inputs to their recorded build baseline.

This is a GET-only diagnostic.  Changed inputs are not built or published and
an ahead/diverged fork is never overwritten. OCR-named upstream cache/patch
branches are existing inputs, not local OCR production.
"""
from __future__ import annotations

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    from . import bha_fork_safe_sync as identity
    from . import build_fork_parsed as parsed
except ImportError:
    import bha_fork_safe_sync as identity
    import build_fork_parsed as parsed


MAX_STATE = 2 * 1024 * 1024


def load_baseline(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_STATE:
        raise ValueError('BHA input baseline is missing or oversized')
    value = json.loads(path.read_bytes())
    if (not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('archives'), dict)
            or set(value['archives']) != {str(i) for i in range(32)}):
        raise ValueError('BHA input baseline must contain all 32 archives')
    return value['archives']


def _row(token: str, archive: int, helper: str, baseline: dict) -> dict:
    repo, upstream_info, mirror_info = identity.identities(token, archive)
    mirror = parsed.branch_revisions(token, parsed.MIRROR_OWNER, repo)
    upstream = parsed.branch_revisions(token, parsed.UPSTREAM_OWNER, repo)
    for branch in (*parsed.INPUT_BRANCHES, parsed.PARSED_BRANCH):
        identity._sha(mirror[branch]); identity._sha(upstream[branch])
    snapshot = parsed.source_snapshot(mirror, helper)
    previous = baseline.get(str(archive))
    if not isinstance(previous, dict) or set(previous) != set(snapshot):
        raise ValueError('BHA input baseline lacks an exact source identity')
    for branch in (*parsed.INPUT_BRANCHES, 'helper'):
        identity._sha(previous[branch])
    if not isinstance(previous['patch_layout'], str):
        raise TypeError('BHA patch layout baseline is invalid')
    return {'archive': archive, 'upstream_repository_id': upstream_info['id'], 'mirror_repository_id': mirror_info['id'],
            'mirror_inputs': snapshot, 'mirror_parsed': mirror['parsed'], 'upstream_parsed': upstream['parsed'],
            'input_changed': snapshot != previous, 'local_build_needed': parsed.needs_local_build(mirror, upstream),
            'upstream_parsed_equal': mirror['parsed'] == upstream['parsed']}


def inspect(token: str, baseline_path: Path, *, ids=None, lookup=_row, helper_lookup=parsed.ref_revision) -> dict:
    if not token:
        raise ValueError('existing GitHub credential is required')
    selected = list(range(32)) if ids is None else ids
    if (not isinstance(selected, list) or not selected or len(selected) > 32
            or any(type(i) is not int or not 0 <= i <= 31 for i in selected)
            or len(set(selected)) != len(selected)):
        raise ValueError('BHA archive selection must contain unique numbers 0 through 31')
    baseline = load_baseline(baseline_path)
    helper = identity._sha(helper_lookup(token, parsed.UPSTREAM_OWNER, 'ocr_helper', 'main'))
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = list(pool.map(lambda number: lookup(token, number, helper, baseline), selected))
    with ThreadPoolExecutor(max_workers=3) as pool:
        second = list(pool.map(lambda number: lookup(token, number, helper, baseline), selected))
    if first != second or helper_lookup(token, parsed.UPSTREAM_OWNER, 'ocr_helper', 'main') != helper:
        raise RuntimeError('BHA source inputs changed during the read-only inspection')
    if any(not isinstance(row, dict) or row.get('archive') != number for number, row in zip(selected, first)):
        raise ValueError('BHA input observations are incomplete')
    return {'version': 1, 'kind': 'bha-input-readonly-report', 'helper_revision': helper, 'archives': first,
            'changed_archives': [row['archive'] for row in first if row['input_changed']],
            'local_build_required': [row['archive'] for row in first if row['local_build_needed']],
            'inputs_observed_twice': True, 'remote_writes': 0, 'build_requested': False, 'publication_ready': False,
            'scope': 'fixed branch identities and baseline only; built parsed bytes and consumer delivery unverified'}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, default=Path('state/bha-parsed-inputs.json'))
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--fail-on-change', action='store_true', help='return nonzero after writing a diagnostic drift report')
    args = parser.parse_args(argv)
    if args.output.resolve() == args.baseline.resolve() or args.output.exists() or args.output.is_symlink():
        raise ValueError('BHA report must be a new path outside its baseline')
    token = os.environ.get('GH_PAT') or os.environ.get('GITHUB_TOKEN', '')
    report = inspect(token, args.baseline)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as output:
        json.dump(report, output, ensure_ascii=False, sort_keys=True)
        output.write('\n')
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 1 if args.fail_on_change and report['changed_archives'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
